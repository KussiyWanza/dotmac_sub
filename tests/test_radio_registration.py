"""Radio MAC capture at install: validation, storage, guards, CRM endpoint."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.auth import ApiKey
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType
from app.models.subscriber import Subscriber
from app.services import radio_registration, unmatched_radio_queue
from app.services.auth import hash_api_key

MAC = "24:A4:3C:AA:BB:01"
MAC_COMPACT = "24a43caabb01"


def _subscription(db_session, subscriber, catalog_offer, mac=None):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        mac_address=mac,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _other_subscriber(db_session):
    other = Subscriber(
        first_name="Other",
        last_name="Customer",
        email=f"other-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(other)
    db_session.flush()
    return other


# ---------------------------------------------------------------------------
# Service behaviour
# ---------------------------------------------------------------------------


class TestRegisterRadioMac:
    def test_register_creates_cpe_row_and_stamps_subscription(
        self, db_session, subscriber, catalog_offer
    ):
        subscription = _subscription(db_session, subscriber, catalog_offer)

        result = radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac="24-a4-3c-aa-bb-01"
        )

        assert result.created is True
        device = result.device
        assert device.subscriber_id == subscriber.id
        assert device.subscription_id == subscription.id
        assert device.device_type == DeviceType.wireless_radio
        assert device.mac_address == MAC  # canonical form
        assert device.uisp_device_id is None
        assert device.installed_at is not None
        assert device.service_address_id == subscription.service_address_id
        # Stamped so the EXISTING uisp_sync matcher links the radio next run.
        assert result.subscription_mac_stamped is True
        db_session.refresh(subscription)
        assert subscription.mac_address == MAC

    def test_invalid_mac_rejected_and_nothing_created(
        self, db_session, subscriber, catalog_offer
    ):
        subscription = _subscription(db_session, subscriber, catalog_offer)

        with pytest.raises(radio_registration.InvalidMacError):
            radio_registration.register_radio_mac(
                db_session, subscription_id=str(subscription.id), mac="not-a-mac"
            )

        assert (
            db_session.query(CPEDevice)
            .filter(CPEDevice.subscriber_id == subscriber.id)
            .count()
            == 0
        )

    def test_unknown_subscription_raises_lookup_error(self, db_session):
        with pytest.raises(LookupError):
            radio_registration.register_radio_mac(
                db_session, subscription_id=str(uuid.uuid4()), mac=MAC
            )
        with pytest.raises(LookupError):
            radio_registration.register_radio_mac(
                db_session, subscription_id="not-a-uuid", mac=MAC
            )

    def test_duplicate_registration_is_idempotent(
        self, db_session, subscriber, catalog_offer
    ):
        subscription = _subscription(db_session, subscriber, catalog_offer)

        first = radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac=MAC
        )
        second = radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac="24a43caabb01"
        )

        assert first.created is True
        assert second.created is False
        assert second.device.id == first.device.id
        assert (
            db_session.query(CPEDevice)
            .filter(CPEDevice.subscriber_id == subscriber.id)
            .count()
            == 1
        )

    def test_same_subscriber_different_service_conflict_opens_review(
        self, db_session, subscriber, catalog_offer
    ):
        first_subscription = _subscription(db_session, subscriber, catalog_offer)
        second_subscription = _subscription(db_session, subscriber, catalog_offer)
        radio_registration.register_radio_mac(
            db_session, subscription_id=str(first_subscription.id), mac=MAC
        )

        with pytest.raises(
            radio_registration.MacConflictError,
            match="another service on this subscriber",
        ):
            radio_registration.register_radio_mac(
                db_session, subscription_id=str(second_subscription.id), mac=MAC
            )

        items = [
            item
            for item in unmatched_radio_queue.open_items(db_session)
            if (item.metadata_ or {}).get("radio_mac") == MAC_COMPACT
        ]
        assert len(items) == 1
        assert items[0].metadata_["conflicting_subscription_id"] == str(
            first_subscription.id
        )
        db_session.refresh(second_subscription)
        assert second_subscription.mac_address is None

    def test_cross_subscriber_conflict_rejected_via_subscription_mac(
        self, db_session, subscriber, catalog_offer
    ):
        other = _other_subscriber(db_session)
        _subscription(db_session, other, catalog_offer, mac=MAC)
        subscription = _subscription(db_session, subscriber, catalog_offer)

        with pytest.raises(radio_registration.MacConflictError):
            radio_registration.register_radio_mac(
                db_session, subscription_id=str(subscription.id), mac=MAC
            )

        assert (
            db_session.query(CPEDevice)
            .filter(CPEDevice.subscriber_id == subscriber.id)
            .count()
            == 0
        )

    def test_cross_subscriber_conflict_rejected_via_cpe_row(
        self, db_session, subscriber, catalog_offer
    ):
        other = _other_subscriber(db_session)
        db_session.add(
            CPEDevice(
                subscriber_id=other.id,
                device_type=DeviceType.wireless_radio,
                mac_address=MAC,
            )
        )
        db_session.flush()
        subscription = _subscription(db_session, subscriber, catalog_offer)

        with pytest.raises(radio_registration.MacConflictError):
            radio_registration.register_radio_mac(
                db_session, subscription_id=str(subscription.id), mac=MAC
            )

    def test_conflict_opens_deduped_queue_item(
        self, db_session, subscriber, catalog_offer
    ):
        other = _other_subscriber(db_session)
        _subscription(db_session, other, catalog_offer, mac=MAC)
        subscription = _subscription(db_session, subscriber, catalog_offer)

        for _ in range(2):
            with pytest.raises(radio_registration.MacConflictError):
                radio_registration.register_radio_mac(
                    db_session, subscription_id=str(subscription.id), mac=MAC
                )

        items = [
            t
            for t in unmatched_radio_queue.open_items(db_session)
            if (t.metadata_ or {}).get("radio_mac") == MAC_COMPACT
        ]
        assert len(items) == 1
        assert items[0].metadata_["reason"] == unmatched_radio_queue.REASON_CONFLICT
        assert items[0].metadata_["occurrences"] == 2

    def test_existing_different_subscription_mac_is_not_overwritten(
        self, db_session, subscriber, catalog_offer
    ):
        subscription = _subscription(
            db_session, subscriber, catalog_offer, mac="AA:BB:CC:00:00:01"
        )

        result = radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac=MAC
        )

        assert result.subscription_mac_stamped is False
        assert result.warnings
        db_session.refresh(subscription)
        assert subscription.mac_address == "AA:BB:CC:00:00:01"

    def test_successful_registration_closes_open_conflict_item(
        self, db_session, subscriber, catalog_offer
    ):
        subscription = _subscription(db_session, subscriber, catalog_offer)
        unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_CONFLICT,
            title="Radio MAC conflict",
            description="stale",
        )

        radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac=MAC
        )

        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None


# ---------------------------------------------------------------------------
# Per-MAC advisory lock: check-then-write is serialized (race guard)
# ---------------------------------------------------------------------------


class TestMacLock:
    def test_lock_key_is_deterministic_and_fits_signed_bigint(self):
        key_a = radio_registration.mac_lock_key(MAC_COMPACT)
        key_b = radio_registration.mac_lock_key(MAC_COMPACT)
        assert key_a == key_b  # sha256-based, stable across processes
        assert -(2**63) <= key_a < 2**63
        assert key_a != radio_registration.mac_lock_key("aabbccddeeff")

    def test_acquire_emits_pg_advisory_xact_lock_on_postgres(self):
        from unittest.mock import MagicMock

        db = MagicMock()
        db.bind.dialect.name = "postgresql"

        radio_registration.acquire_mac_lock(db, MAC_COMPACT)

        assert db.execute.call_count == 1
        statement, params = db.execute.call_args.args
        assert "pg_advisory_xact_lock" in str(statement)
        assert params == {"key": radio_registration.mac_lock_key(MAC_COMPACT)}

    def test_acquire_is_noop_on_sqlite(self, db_session):
        # Must not raise (SQLite has no advisory locks; single-writer anyway).
        assert radio_registration.acquire_mac_lock(db_session, MAC_COMPACT) is None

    def test_register_reads_after_lock_sees_concurrent_winner(
        self, db_session, subscriber, catalog_offer, monkeypatch
    ):
        """The idempotency check runs AFTER the lock is acquired.

        Simulates losing the race: while this request waits on the per-MAC
        lock, a concurrent identical registration commits its row. When the
        lock is granted, the existence check must observe that row and return
        it (created=False) instead of inserting a duplicate.
        """
        subscription = _subscription(db_session, subscriber, catalog_offer)

        def _lock_granted_after_competitor_won(db, mac_compact):
            db.add(
                CPEDevice(
                    subscriber_id=subscriber.id,
                    device_type=DeviceType.wireless_radio,
                    mac_address=MAC,
                )
            )
            db.flush()

        monkeypatch.setattr(
            radio_registration,
            "acquire_mac_lock",
            _lock_granted_after_competitor_won,
        )

        result = radio_registration.register_radio_mac(
            db_session, subscription_id=str(subscription.id), mac=MAC
        )

        assert result.created is False
        assert (
            db_session.query(CPEDevice)
            .filter(CPEDevice.subscriber_id == subscriber.id)
            .count()
            == 1
        )

    def test_open_item_reads_after_lock_sees_concurrent_ticket(
        self, db_session, monkeypatch
    ):
        """open_item's find-then-create runs under the same per-MAC lock."""
        from app.models.support import Ticket, TicketChannel, TicketStatus

        def _lock_granted_after_competitor_won(db, mac_compact):
            db.add(
                Ticket(
                    title="competitor",
                    status=TicketStatus.open.value,
                    channel=TicketChannel.api,
                    ticket_type=unmatched_radio_queue.TICKET_TYPE,
                    metadata_={
                        "opened_by": "unmatched_radio_queue",
                        "radio_mac": mac_compact,
                        "occurrences": 1,
                    },
                )
            )
            db.flush()

        monkeypatch.setattr(
            radio_registration,
            "acquire_mac_lock",
            _lock_granted_after_competitor_won,
        )

        ticket, created = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )

        assert created is False
        assert ticket.title == "competitor"
        assert (
            db_session.query(Ticket)
            .filter(Ticket.ticket_type == unmatched_radio_queue.TICKET_TYPE)
            .count()
            == 1
        )


# ---------------------------------------------------------------------------
# Admin web route: registration + permission guard
# ---------------------------------------------------------------------------


class TestAdminRoute:
    def test_route_registered(self):
        from app.web.admin.catalog import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert (
            "/catalog/subscriptions/{subscription_id}/register-radio-mac",
            "POST",
        ) in routes

    def test_route_mounted_on_admin(self):
        from app.web.admin import router as admin_router

        paths = {getattr(route, "path", "") for route in admin_router.routes}
        assert (
            "/admin/catalog/subscriptions/{subscription_id}/register-radio-mac" in paths
        )

    def test_route_declares_catalog_write_guard(self):
        from app.web.admin.catalog import router

        route = next(
            r
            for r in router.routes
            if getattr(r, "path", "")
            == "/catalog/subscriptions/{subscription_id}/register-radio-mac"
        )
        names = set()
        stack = [route.dependant]
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None:
                names.add(getattr(call, "__name__", ""))
            stack.extend(getattr(dep, "dependencies", []) or [])
        assert "_require_permission" in names

    def test_post_registers_and_redirects(self, db_session, subscriber, catalog_offer):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.db import get_db
        from app.web.admin.catalog import router

        subscription = _subscription(db_session, subscriber, catalog_offer)
        db_session.commit()

        app = FastAPI()
        app.include_router(router, prefix="/admin")

        def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        for route in router.routes:
            dependant = getattr(route, "dependant", None)
            for dependency in getattr(dependant, "dependencies", []) or []:
                call = getattr(dependency, "call", None)
                if getattr(call, "__name__", "") == "_require_permission":
                    app.dependency_overrides[call] = lambda: {"roles": ["admin"]}
        client = TestClient(app)

        response = client.post(
            f"/admin/catalog/subscriptions/{subscription.id}/register-radio-mac",
            data={"mac_address": MAC},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "notice=" in response.headers["location"]
        device = (
            db_session.query(CPEDevice)
            .filter(CPEDevice.subscriber_id == subscriber.id)
            .one()
        )
        assert device.mac_address == MAC

        # Invalid MAC redirects back with an error, creates nothing.
        response = client.post(
            f"/admin/catalog/subscriptions/{subscription.id}/register-radio-mac",
            data={"mac_address": "bogus"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]


# ---------------------------------------------------------------------------
# CRM API endpoint (bearer-guarded, install-time field app call)
# ---------------------------------------------------------------------------

TOKEN = "crm-test-token"


@pytest.fixture()
def crm_auth(db_session):
    db_session.add(
        ApiKey(
            label="crm-radio-test",
            key_hash=hash_api_key(TOKEN),
            scopes=["integration:crm"],
            is_active=True,
        )
    )
    db_session.commit()
    return TOKEN


def _call(func, *args, **kwargs):
    try:
        body = func(*args, **kwargs)
    except HTTPException as exc:
        return exc.status_code, {"detail": exc.detail}
    return 201, body


class TestCrmEndpoint:
    def test_requires_scoped_api_key(self, db_session, crm_auth):
        from app.api.crm import require_crm_service_auth

        with pytest.raises(HTTPException) as excinfo:
            require_crm_service_auth(request=None, x_api_key=None, db=db_session)
        assert excinfo.value.status_code == 401
        with pytest.raises(HTTPException) as excinfo:
            require_crm_service_auth(
                request=None,
                x_api_key="wrong-token",
                db=db_session,
            )
        assert excinfo.value.status_code == 401
        assert (
            require_crm_service_auth(request=None, x_api_key=TOKEN, db=db_session)
            is None
        )

    def test_route_declares_api_key_guard(self):
        from app.api.crm import router

        route = next(
            r
            for r in router.routes
            if getattr(r, "path", "")
            == "/crm/subscriptions/{subscription_id}/radio-mac"
        )
        names = {
            getattr(getattr(dep, "call", None), "__name__", "")
            for dep in route.dependant.dependencies
        }
        assert "require_crm_service_auth" in names

    def test_register_created(self, db_session, subscriber, catalog_offer):
        from app.api.crm import register_subscription_radio_mac

        subscription = _subscription(db_session, subscriber, catalog_offer)
        status_code, body = _call(
            register_subscription_radio_mac,
            subscription_id=str(subscription.id),
            payload={"mac_address": MAC},
            db=db_session,
        )
        assert status_code == 201
        data = body["data"]
        assert data["mac_address"] == MAC
        assert data["created"] is True
        assert data["subscription_mac_stamped"] is True
        assert data["uisp_confirmed"] is False
        assert data["subscriber_id"] == str(subscriber.id)

        # Idempotent repeat.
        status_code, body = _call(
            register_subscription_radio_mac,
            subscription_id=str(subscription.id),
            payload={"mac": MAC.lower()},
            db=db_session,
        )
        assert status_code == 201
        assert body["data"]["created"] is False

    def test_register_validation_errors(self, db_session, subscriber, catalog_offer):
        from app.api.crm import register_subscription_radio_mac

        subscription = _subscription(db_session, subscriber, catalog_offer)

        status_code, body = _call(
            register_subscription_radio_mac,
            subscription_id=str(subscription.id),
            payload={},
            db=db_session,
        )
        assert status_code == 400
        assert "mac_address" in body["detail"]["errors"]

        status_code, _ = _call(
            register_subscription_radio_mac,
            subscription_id=str(subscription.id),
            payload={"mac_address": "zz:zz"},
            db=db_session,
        )
        assert status_code == 400

        status_code, _ = _call(
            register_subscription_radio_mac,
            subscription_id=str(uuid.uuid4()),
            payload={"mac_address": MAC},
            db=db_session,
        )
        assert status_code == 404

    def test_register_conflict_is_409(self, db_session, subscriber, catalog_offer):
        from app.api.crm import register_subscription_radio_mac

        other = _other_subscriber(db_session)
        _subscription(db_session, other, catalog_offer, mac=MAC)
        subscription = _subscription(db_session, subscriber, catalog_offer)

        status_code, _ = _call(
            register_subscription_radio_mac,
            subscription_id=str(subscription.id),
            payload={"mac_address": MAC},
            db=db_session,
        )
        assert status_code == 409
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is not None
