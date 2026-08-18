"""Unit tests for the self-scoped customer endpoints in app/api/me.py.

They verify the two things the endpoint layer is responsible for: rejecting
non-subscriber principals (403) and forcing the caller's own subscriber_id as
the scope passed to the underlying list services.
"""

import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import me as me_api


def _subscriber_principal():
    sid = str(uuid.uuid4())
    return {"principal_type": "subscriber", "subscriber_id": sid}


def _system_user_principal():
    return {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}


def test_customer_service_change_routes_are_canonical_without_plan_change_alias():
    paths = {getattr(route, "path", "") for route in me_api.router.routes}
    assert "/me/subscriptions/{subscription_id}/service-change" in paths
    assert "/me/subscriptions/{subscription_id}/service-change/quote" in paths
    assert "/me/subscriptions/{subscription_id}/plan-change" not in paths
    assert "/me/subscriptions/{subscription_id}/plan-change/quote" not in paths


def test_customer_device_command_routes_are_self_scoped():
    paths = {getattr(route, "path", "") for route in me_api.router.routes}
    assert "/me/subscriptions/{subscription_id}/device/reboot" in paths
    assert "/me/subscriptions/{subscription_id}/device/wifi" in paths


def test_reboot_device_forces_principal_scope(monkeypatch):
    from app.services import customer_device_commands

    principal = _subscriber_principal()
    subscription_id = uuid.uuid4()
    captured = {}

    def fake(db, *, subscriber_id, subscription_id, actor_id):
        captured.update(
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            actor_id=actor_id,
        )
        return customer_device_commands.CustomerDeviceCommandOutcome(
            command=customer_device_commands.CustomerDeviceCommandKind.reboot,
            status=customer_device_commands.CustomerDeviceCommandStatus.succeeded,
            subscription_id=subscription_id,
            device_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),
            message="Restart completed",
        )

    monkeypatch.setattr(customer_device_commands, "reboot_subscription_device", fake)
    outcome = me_api.reboot_my_subscription_device(
        subscription_id, db=None, principal=principal
    )

    assert outcome.message == "Restart completed"
    assert captured["subscriber_id"] == uuid.UUID(principal["subscriber_id"])
    assert captured["subscription_id"] == subscription_id


def test_wifi_update_forces_principal_scope_and_queues_with_idempotency(monkeypatch):
    from app.schemas.customer_device_commands import CustomerWifiUpdateRequest
    from app.services import customer_device_commands

    principal = _subscriber_principal()
    subscription_id = uuid.uuid4()
    captured = {}

    @contextmanager
    def fake_owner_session(db):
        yield db

    def fake(
        db,
        *,
        subscriber_id,
        subscription_id,
        context,
        ssid,
        password,
    ):
        captured.update(
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            context=context,
            ssid=ssid,
            password=password,
        )
        return customer_device_commands.CustomerDeviceCommandOutcome(
            command=customer_device_commands.CustomerDeviceCommandKind.wifi_update,
            status=customer_device_commands.CustomerDeviceCommandStatus.queued,
            subscription_id=subscription_id,
            device_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),
            message="WiFi update queued.",
        )

    monkeypatch.setattr(me_api, "owner_session", fake_owner_session)
    monkeypatch.setattr(customer_device_commands, "update_subscription_wifi", fake)
    outcome = me_api.update_my_subscription_wifi(
        subscription_id,
        CustomerWifiUpdateRequest(
            ssid="Home Network",
            password="password123",
            idempotency_key="mobile-request-1",
        ),
        db=object(),
        principal=principal,
    )

    assert outcome.status is customer_device_commands.CustomerDeviceCommandStatus.queued
    assert captured["subscriber_id"] == uuid.UUID(principal["subscriber_id"])
    assert captured["subscription_id"] == subscription_id
    assert captured["context"].scope == "customer:device:wifi"
    assert captured["context"].idempotency_key == "mobile-request-1"


def test_subscriber_id_helper_rejects_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api._subscriber_id(_system_user_principal())
    assert exc.value.status_code == 403


def test_my_invoices_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_list_response(
        db, account_id, status, is_active, order_by, order_dir, limit, offset
    ):
        captured["account_id"] = account_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.billing_service.invoices, "list_response", fake_list_response
    )

    me_api.my_invoices(
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=None,
        principal=principal,
    )
    # The caller's own id must be forced as the account scope.
    assert captured["account_id"] == principal["subscriber_id"]


def test_my_invoices_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_invoices(
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_my_subscriptions_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_list_response(
        db, subscriber_id, offer_id, status, order_by, order_dir, limit, offset
    ):
        captured["subscriber_id"] = subscriber_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.catalog_service.subscriptions, "list_response", fake_list_response
    )

    me_api.my_subscriptions(
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=None,
        principal=principal,
    )
    assert captured["subscriber_id"] == principal["subscriber_id"]


def test_my_accounting_sessions_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_accounting_sessions(
            limit=50, offset=0, db=None, principal=_system_user_principal()
        )
    assert exc.value.status_code == 403


def test_my_notifications_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake(db, subscriber_id, limit, offset):
        captured["subscriber_id"] = subscriber_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.notification_service.notifications,
        "list_response_for_subscriber",
        fake,
    )

    def fake_apply(db, *, subscriber_id, notifications):
        captured["read_state_subscriber_id"] = subscriber_id
        captured["notifications"] = notifications
        return notifications

    monkeypatch.setattr(
        me_api.customer_notifications_service,
        "apply_notification_read_state",
        fake_apply,
    )
    me_api.my_notifications(limit=50, offset=0, db=None, principal=principal)
    assert captured["subscriber_id"] == principal["subscriber_id"]
    assert captured["read_state_subscriber_id"] == principal["subscriber_id"]
    assert captured["notifications"] == []


def test_my_notifications_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_notifications(
            limit=50, offset=0, db=None, principal=_system_user_principal()
        )
    assert exc.value.status_code == 403


def test_my_notifications_mark_read_scopes_to_caller(monkeypatch):
    from app.schemas.notification import CustomerNotificationReadRequest

    principal = _subscriber_principal()
    notification_id = uuid.uuid4()
    captured = {}

    def fake_mark(db, *, subscriber_id, notification_ids, all_visible=False) -> int:
        captured["subscriber_id"] = subscriber_id
        captured["notification_ids"] = notification_ids
        captured["all_visible"] = all_visible
        return 1

    monkeypatch.setattr(
        me_api.customer_notifications_service,
        "mark_api_notifications_read",
        fake_mark,
    )

    response = me_api.my_notifications_mark_read(
        CustomerNotificationReadRequest(notification_ids=[notification_id]),
        db=None,
        principal=principal,
    )

    assert response.marked == 1
    assert captured == {
        "subscriber_id": principal["subscriber_id"],
        "notification_ids": [notification_id],
        "all_visible": False,
    }


def test_my_notifications_mark_read_403_for_non_subscriber():
    from app.schemas.notification import CustomerNotificationReadRequest

    with pytest.raises(HTTPException) as exc:
        me_api.my_notifications_mark_read(
            CustomerNotificationReadRequest(all_visible=True),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_topup_initiate_403_for_non_subscriber():
    from app.schemas.billing import TopupInitiateRequest

    with pytest.raises(HTTPException) as exc:
        me_api.my_topup_initiate(
            TopupInitiateRequest(amount=5000, preview_fingerprint="x" * 64),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_plan_change_submit_403_for_non_subscriber():
    from app.schemas.catalog import PlanChangeSubmitRequest

    with pytest.raises(HTTPException) as exc:
        me_api.my_plan_change_submit(
            subscription_id="s1",
            payload=PlanChangeSubmitRequest(
                offer_id=uuid.uuid4(),
                preview_fingerprint="x" * 64,
                idempotency_key="test-plan-change",
            ),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_topup_initiate_translates_value_error(monkeypatch):
    from app.schemas.billing import TopupInitiateRequest

    monkeypatch.setattr(
        me_api,
        "_customer",
        lambda db, principal: {"account_id": "x", "subscriber_id": "x"},
    )

    def _boom(db, customer, amount, **kwargs):
        raise ValueError("Top-up amount must be at least ₦1,000.00")

    monkeypatch.setattr(me_api.customer_payments, "create_topup_intent", _boom)
    with pytest.raises(HTTPException) as exc:
        me_api.my_topup_initiate(
            TopupInitiateRequest(amount=1, preview_fingerprint="x" * 64),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400


def test_topup_initiate_400_with_friendly_saved_card_charge_error(monkeypatch):
    from app.schemas.billing import TopupInitiateRequest

    monkeypatch.setattr(
        me_api,
        "_customer",
        lambda db, principal: {
            "account_id": "x",
            "subscriber_id": "x",
            "username": "customer@example.com",
        },
    )

    def _boom(db, customer, amount, **kwargs):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(me_api.customer_payments, "create_topup_intent", _boom)

    with pytest.raises(HTTPException) as exc:
        me_api.my_topup_initiate(
            TopupInitiateRequest(
                amount=Decimal("5000"),
                payment_method_id=uuid.uuid4(),
                idempotency_key="idem-1",
                preview_fingerprint="x" * 64,
            ),
            db=None,
            principal=_subscriber_principal(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        "We could not charge that saved card. Please use another payment method "
        "or try again later."
    )


def test_topup_verify_surfaces_card_save_failure_without_failing(monkeypatch):
    from app.schemas.billing import TopupVerifyRequest

    monkeypatch.setattr(
        me_api,
        "_customer",
        lambda db, principal: {"account_id": "x", "subscriber_id": "x"},
    )
    monkeypatch.setattr(
        me_api.customer_payments,
        "verify_and_record_topup",
        lambda db, customer, reference: {
            "amount": Decimal("5000.00"),
            "already_recorded": False,
            "available_balance": Decimal("7500.00"),
            "credit_added": Decimal("5000.00"),
        },
    )

    def _capture_boom(db, account_id, reference, provider):
        raise RuntimeError("provider token missing")

    monkeypatch.setattr(
        me_api.customer_cards,
        "capture_card_after_payment",
        _capture_boom,
    )

    resp = me_api.my_topup_verify(
        TopupVerifyRequest(reference="ref_topup", save_card=True),
        db=None,
        principal=_subscriber_principal(),
    )

    assert resp.reference == "ref_topup"
    assert resp.card_saved is False
    assert resp.card_save_message == (
        "Payment was recorded, but we could not save this card. "
        "You can add a card from Payment Methods."
    )


def test_my_invoice_detail_404_when_not_owned(monkeypatch):
    principal = _subscriber_principal()

    class _Invoice:
        account_id = uuid.uuid4()  # a different owner

    monkeypatch.setattr(
        me_api.billing_service.invoices, "get", lambda db, invoice_id: _Invoice()
    )
    with pytest.raises(HTTPException) as exc:
        me_api.my_invoice(invoice_id=str(uuid.uuid4()), db=None, principal=principal)
    assert exc.value.status_code == 404
