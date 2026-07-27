from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPortalNotificationsPage:
    def test_notifications_page_merges_event_queue_and_customer_notification_events(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
            NotificationTemplate,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.phone = "+2348000000000"
        template = NotificationTemplate(
            name="Invoice Created",
            code="invoice_created",
            channel=NotificationChannel.email,
            body="Invoice body",
            is_active=True,
        )
        db_session.add(template)
        db_session.flush()

        queued = Notification(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            body="Invoice ready",
            status=NotificationStatus.delivered,
        )
        direct = CustomerNotificationEvent(
            entity_type="service_order",
            entity_id=subscriber.id,
            channel="sms",
            recipient=subscriber.phone,
            message="Technician dispatched",
            status=CustomerNotificationStatus.sent,
        )
        db_session.add_all([queued, direct])
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 2
        entity_types = {item.entity_type for item in page["notifications"]}
        assert "invoice_created" in entity_types
        assert "service_order" in entity_types

    def test_notifications_preview_returns_recent_items_and_total(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_preview

        subscriber.phone = "+2348000000000"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    body="Billing reminder",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Technician dispatched",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        preview = get_notifications_preview(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            limit=1,
        )

        assert preview["recent_notifications_total"] == 2
        assert preview["unread_notifications_count"] == 2
        assert preview["has_recent_notifications"] is True
        assert len(preview["recent_notifications"]) == 1

    def test_notifications_page_resolves_account_id_only_session(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        db_session.add(
            Notification(
                subscriber_id=subscriber.id,
                channel=NotificationChannel.email,
                recipient=subscriber.email,
                event_type="invoice_sent",
                category="billing",
                body="Account-scoped session notice",
                status=NotificationStatus.delivered,
            )
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"account_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert page["notifications"][0].message == "Account-scoped session notice"

    def test_mark_notifications_read_updates_page_and_preview_counts(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import (
            get_notifications_page,
            get_notifications_preview,
            mark_notifications_read,
        )

        notice = Notification(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            event_type="invoice_paid",
            category="billing",
            body="Payment received",
            status=NotificationStatus.delivered,
        )
        db_session.add(notice)
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )
        read_key = page["notifications"][0].read_key

        marked = mark_notifications_read(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            read_key=read_key,
        )
        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )
        preview = get_notifications_preview(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            limit=5,
        )

        assert marked == 1
        assert page["notifications"][0].is_read is True
        assert page["unread_notifications_count"] == 0
        assert preview["unread_notifications_count"] == 0

    def test_mobile_read_mutation_updates_web_and_api_read_state(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.schemas.notification import CustomerInboxNotificationRead
        from app.services.customer_portal_notifications import (
            apply_notification_read_state,
            get_notifications_page,
            mark_api_notifications_read,
        )
        from app.services.notification import notifications

        notice = Notification(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.push,
            recipient=subscriber.email,
            event_type="service_restored",
            category="service",
            body="Your service is back online",
            status=NotificationStatus.delivered,
        )
        db_session.add(notice)
        db_session.commit()

        api_response = notifications.list_response_for_subscriber(
            db_session, subscriber.id, 50, 0
        )
        apply_notification_read_state(
            db_session,
            subscriber_id=str(subscriber.id),
            notifications=api_response["items"],
        )
        assert api_response["items"][0].is_read is False

        marked = mark_api_notifications_read(
            db_session,
            subscriber_id=str(subscriber.id),
            notification_ids=[notice.id],
        )
        portal_page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )
        refreshed_api = notifications.list_response_for_subscriber(
            db_session, subscriber.id, 50, 0
        )
        apply_notification_read_state(
            db_session,
            subscriber_id=str(subscriber.id),
            notifications=refreshed_api["items"],
        )

        assert marked == 1
        assert portal_page["notifications"][0].is_read is True
        assert portal_page["unread_notifications_count"] == 0
        assert refreshed_api["items"][0].is_read is True
        assert (
            CustomerInboxNotificationRead.model_validate(
                refreshed_api["items"][0]
            ).is_read
            is True
        )

    def test_mobile_read_mutation_ignores_another_subscribers_ids(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_notifications import (
            apply_notification_read_state,
            mark_api_notifications_read,
        )

        other = Subscriber(
            first_name="Other",
            last_name="Customer",
            email="other-read-state@example.com",
        )
        db_session.add(other)
        db_session.flush()
        other_notice = Notification(
            subscriber_id=other.id,
            channel=NotificationChannel.email,
            recipient=other.email,
            body="Other customer only",
            status=NotificationStatus.delivered,
        )
        db_session.add(other_notice)
        db_session.commit()

        marked = mark_api_notifications_read(
            db_session,
            subscriber_id=str(subscriber.id),
            notification_ids=[other_notice.id],
        )
        apply_notification_read_state(
            db_session,
            subscriber_id=str(other.id),
            notifications=[other_notice],
        )

        assert marked == 0
        assert other_notice.is_read is False

    def test_notifications_page_prefers_subscriber_id_and_hides_non_visible_statuses(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_notifications import get_notifications_page

        other_subscriber = Subscriber(
            first_name="Other",
            last_name="User",
            email="other@example.com",
            phone=subscriber.phone,
        )
        db_session.add(other_subscriber)
        db_session.flush()

        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient="old-email@example.com",
                    event_type="invoice_paid",
                    category="billing",
                    body="Delivered to owned subscriber",
                    status=NotificationStatus.delivered,
                ),
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Queued row should stay hidden",
                    status=NotificationStatus.queued,
                ),
                Notification(
                    subscriber_id=other_subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Recipient collision should stay hidden",
                    status=NotificationStatus.delivered,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert page["notifications"][0].message == "Delivered to owned subscriber"

    def test_notifications_page_converts_legacy_html_body_to_text(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        db_session.add(
            Notification(
                subscriber_id=subscriber.id,
                channel=NotificationChannel.email,
                recipient=subscriber.email,
                event_type="invoice_sent",
                category="billing",
                body="<p>Your <strong>invoice</strong> is ready.</p>",
                status=NotificationStatus.delivered,
            )
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert page["notifications"][0].message == "Your invoice is ready."

    def test_notifications_page_respects_billing_and_sms_preferences(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.metadata_ = {
            "billing_notifications": False,
            "sms_updates": False,
            "push_notifications": False,
        }
        subscriber.phone = "+2348000000011"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Billing email",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order_completed",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Service order completed",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 0


class TestPortalNotificationDropdown:
    def test_dropdown_has_separate_read_actions_and_bottom_view_all_link(
        self,
    ) -> None:
        source = (REPO_ROOT / "templates/layouts/customer.html").read_text(
            encoding="utf-8"
        )
        menu = source.split('id="customer-notifications-menu"', maxsplit=1)[1].split(
            "<!-- Dark mode toggle", maxsplit=1
        )[0]
        notification_loop = menu.index("{% for notification in recent_notifications %}")
        notification_link_close = menu.index("</a>", notification_loop)
        read_action = menu.index('name="read_key"', notification_loop)

        assert "Mark all as read" in menu
        assert 'name="all_visible" value="true"' in menu
        assert "{% if not notification.is_read" in menu
        assert "Mark as read" in menu
        assert 'name="return_to"' in menu
        assert notification_link_close < read_action
        assert menu.index("Mark all as read") < menu.index("max-h-96")
        assert menu.rindex("View all") > menu.index("{% endfor %}")

    def test_mark_read_route_returns_to_current_portal_page(self) -> None:
        from app.web.customer.routes import customer_notifications_mark_read

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "subscriber-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_notifications_service"
                ".mark_notifications_read"
            ) as mark_read,
        ):
            response = customer_notifications_mark_read(
                request=request,
                read_key="notification:notice-1",
                all_visible=False,
                return_to="/portal/billing?tab=history",
                db=db,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/portal/billing?tab=history"
        mark_read.assert_called_once_with(
            db,
            customer,
            read_key="notification:notice-1",
            all_visible=False,
        )

    def test_mark_read_route_rejects_external_return_target(self) -> None:
        from app.web.customer.routes import customer_notifications_mark_read

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value={"subscriber_id": "subscriber-1"},
            ),
            patch(
                "app.web.customer.routes.customer_notifications_service"
                ".mark_notifications_read"
            ),
        ):
            response = customer_notifications_mark_read(
                request=MagicMock(),
                read_key=None,
                all_visible=True,
                return_to="https://example.com/steal-session",
                db=MagicMock(),
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/portal/notifications"


class TestCustomerProfileNotifications:
    def test_update_customer_profile_persists_preferences_and_emits_subscriber_updated(
        self, db_session, subscriber
    ) -> None:
        from app.services.events.types import EventType
        from app.services.web_customer_actions import update_customer_profile

        with patch("app.services.subscriber.emit_event") as emit_event_mock:
            updated = update_customer_profile(
                db_session,
                subscriber_id=str(subscriber.id),
                first_name="Updated",
                last_name="Customer",
                email="updated@example.com",
                phone="+2348000000012",
                billing_notifications=False,
                sms_updates=True,
                push_notifications=False,
                service_notifications=False,
                account_notifications=True,
                usage_notifications=False,
                general_notifications=True,
                locale="en-NG",
            )

        assert updated is not None
        assert updated.email == "updated@example.com"
        assert updated.phone == "+2348000000012"
        assert (updated.metadata_ or {}).get("billing_notifications") is False
        assert (updated.metadata_ or {}).get("sms_updates") is True
        assert (updated.metadata_ or {}).get("push_notifications") is False
        assert (updated.metadata_ or {}).get("service_notifications") is False
        assert (updated.metadata_ or {}).get("usage_notifications") is False
        assert updated.locale == "en-NG"
        assert emit_event_mock.call_args.args[1] == EventType.subscriber_updated

    def test_customer_update_profile_route_passes_notification_preferences(
        self,
    ) -> None:
        from app.web.customer.routes import customer_update_profile

        request = MagicMock()
        customer = {"subscriber_id": "sub-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.services.web_customer_actions.update_customer_profile"
            ) as update_mock,
        ):
            response = customer_update_profile(
                request=request,
                first_name="Updated",
                last_name="Customer",
                email="updated@example.com",
                phone="+2348000000012",
                billing_notifications=False,
                sms_updates=True,
                push_notifications=False,
                service_notifications=False,
                account_notifications=True,
                usage_notifications=False,
                general_notifications=True,
                locale="en-NG",
                db=MagicMock(),
            )

        assert response.status_code == 303
        kwargs = update_mock.call_args.kwargs
        assert kwargs["billing_notifications"] is False
        assert kwargs["sms_updates"] is True
        assert kwargs["push_notifications"] is False
        assert kwargs["service_notifications"] is False
        assert kwargs["usage_notifications"] is False
        assert kwargs["locale"] == "en-NG"
