"""Unit tests for the self-scoped reseller endpoints in app/api/reseller.py.

They verify the endpoint layer's responsibilities: reject non-reseller
principals (403), force the caller's own reseller_id as the scope passed to the
services, and surface a foreign account (service returns None) as 404.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import reseller as reseller_api


def _subscriber_principal():
    return {"principal_type": "subscriber", "subscriber_id": str(uuid.uuid4())}


def _system_user_principal():
    return {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}


def test_reseller_id_rejects_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        reseller_api._reseller_id(None, _system_user_principal())
    assert exc.value.status_code == 403


def test_reseller_id_rejects_subscriber_without_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api._reseller_id(None, _subscriber_principal())
    assert exc.value.status_code == 403


def test_reseller_id_returns_for_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    assert reseller_api._reseller_id(None, _subscriber_principal()) == "reseller-1"


def test_accounts_scopes_to_caller_reseller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )

    def fake_list(db, reseller_id, limit, offset, search, **kwargs):
        captured["reseller_id"] = reseller_id
        return []

    monkeypatch.setattr(reseller_api.reseller_portal, "list_accounts", fake_list)
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "count_accounts",
        lambda db, rid, search, **kwargs: 0,
    )

    out = reseller_api.my_reseller_accounts(
        search=None, limit=50, offset=0, db=None, principal=principal
    )
    assert captured["reseller_id"] == "reseller-1"
    assert out == {"items": [], "count": 0, "limit": 50, "offset": 0}


def test_accounts_403_for_non_reseller():
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_accounts(
            search=None,
            limit=50,
            offset=0,
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_account_detail_404_for_foreign_account(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    # The service returns None for an account that isn't this reseller's.
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_account(
            account_id=str(uuid.uuid4()), db=None, principal=_subscriber_principal()
        )
    assert exc.value.status_code == 404


def test_service_change_quote_reuses_owner_after_managed_account_scope(monkeypatch):
    principal = _subscriber_principal()
    account_id = str(uuid.uuid4())
    subscription_id = str(uuid.uuid4())
    target_address_id = str(uuid.uuid4())
    captured = {}
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "owned_account",
        lambda db, reseller_id, requested_id: SimpleNamespace(id=account_id),
    )

    def fake_quote(db, customer, sub_id, offer_id, **kwargs):
        captured.update(
            customer=customer,
            subscription_id=sub_id,
            offer_id=offer_id,
            target_service_address_id=kwargs.get("target_service_address_id"),
        )
        return {"delivery_mode": "field_migration"}

    monkeypatch.setattr(
        reseller_api.customer_changes, "get_plan_change_quote", fake_quote
    )

    result = reseller_api.reseller_service_change_quote(
        account_id,
        subscription_id,
        offer_id="offer-1",
        target_service_address_id=target_address_id,
        db=None,
        principal=principal,
    )

    assert result == {"delivery_mode": "field_migration"}
    assert captured == {
        "customer": {
            "account_id": account_id,
            "subscriber_id": principal["subscriber_id"],
        },
        "subscription_id": subscription_id,
        "offer_id": "offer-1",
        "target_service_address_id": target_address_id,
    }


def test_service_change_quote_rejects_foreign_managed_account(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "owned_account",
        lambda db, reseller_id, account_id: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.reseller_service_change_quote(
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            offer_id="offer-1",
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 404


def test_account_tickets_404_for_foreign_account(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "res-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_account_tickets(
            account_id=str(uuid.uuid4()),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 404


def test_account_tickets_uses_native_projection(monkeypatch):
    reseller_id = uuid.uuid4()
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: str(reseller_id),
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: {"id": account_id},
    )

    ticket = SimpleNamespace(
        to_dict=lambda: {
            "id": "native-1",
            "subject": "No internet",
            "status": "open",
        }
    )
    monkeypatch.setattr(
        reseller_api.reseller_ticket_projection,
        "native_account_ticket_summaries",
        lambda db, *, query: (ticket,),
    )

    out = reseller_api.my_reseller_account_tickets(
        account_id=str(uuid.uuid4()),
        db=None,
        principal=_subscriber_principal(),
    )
    assert out == {
        "items": [
            {
                "id": "native-1",
                "subject": "No internet",
                "status": "open",
            }
        ],
        "source": "native",
    }


def test_profile_403_for_non_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_profile(db=None, principal=_subscriber_principal())
    assert exc.value.status_code == 403


def test_profile_roundtrip_and_update(db_session, monkeypatch):
    from app.models.subscriber import Reseller, Subscriber, UserType

    sub = Subscriber(
        first_name="Res",
        last_name="Eller",
        email="reseller.profile@example.com",
        user_type=UserType.reseller,
    )
    reseller = Reseller(name="Acme Networks", code="ACME")
    db_session.add_all([sub, reseller])
    db_session.commit()

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: str(reseller.id),
    )
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}

    out = reseller_api.my_reseller_profile(db=db_session, principal=principal)
    assert out["name"] == "Acme Networks"
    assert out["mfa_enabled"] is False

    updated = reseller_api.my_reseller_profile_update(
        payload=reseller_api.ResellerProfileUpdate(
            contact_email="  ops@acme.example  ", contact_phone=""
        ),
        db=db_session,
        principal=principal,
    )
    assert updated["contact_email"] == "ops@acme.example"
    assert updated["contact_phone"] is None  # blank clears the field


def test_mfa_setup_and_confirm_flow(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    from app.models.subscriber import Reseller, Subscriber, UserType

    sub = Subscriber(
        first_name="Mfa",
        last_name="User",
        email="reseller.mfa@example.com",
        user_type=UserType.reseller,
    )
    reseller = Reseller(name="Mfa Networks")
    db_session.add_all([sub, reseller])
    db_session.commit()

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: str(reseller.id),
    )
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}

    setup = reseller_api.my_reseller_mfa_setup(db=db_session, principal=principal)
    assert setup["method_id"] and setup["secret"] and setup["otpauth_uri"]

    # Wrong code -> 400, method stays unverified.
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_mfa_confirm(
            payload=reseller_api.MfaConfirmRequest(
                method_id=setup["method_id"], code="000000"
            ),
            db=db_session,
            principal=principal,
        )
    assert exc.value.status_code == 400

    # Correct TOTP -> enabled.
    import pyotp

    code = pyotp.TOTP(setup["secret"]).now()
    out = reseller_api.my_reseller_mfa_confirm(
        payload=reseller_api.MfaConfirmRequest(method_id=setup["method_id"], code=code),
        db=db_session,
        principal=principal,
    )
    assert out["mfa_enabled"] is True


def test_billing_endpoints_scope_and_translate_errors(monkeypatch):
    from app.services import reseller_portal_billing

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "res-1",
    )
    captured = {}

    def _summary(db, rid):
        captured["summary_rid"] = rid
        return {"total_outstanding": 5}

    monkeypatch.setattr(
        reseller_portal_billing, "get_billing_account_summary", _summary
    )

    def _activity(db, rid, summary=None, **kwargs):
        captured["activity_rid"] = rid
        return [{"direction": "credit", "amount": 5}]

    monkeypatch.setattr(reseller_portal_billing, "account_activity", _activity)
    out = reseller_api.my_reseller_billing(db=None, principal=_subscriber_principal())
    assert out == {
        "total_outstanding": 5,
        "account_activity": [{"direction": "credit", "amount": 5}],
    }
    assert captured["summary_rid"] == "res-1"
    assert captured["activity_rid"] == "res-1"

    def _bad_amount(db, rid, amount, **kwargs):
        raise ValueError("Payment amount must be greater than 0")

    monkeypatch.setattr(
        reseller_portal_billing, "start_consolidated_payment", _bad_amount
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_pay_intent(
            payload=reseller_api.PayIntentRequest(amount="0"),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400

    def _foreign_ref(db, rid, reference, provider=None):
        raise ValueError("Payment reference was not issued for this billing account")

    monkeypatch.setattr(
        reseller_portal_billing,
        "verify_and_record_consolidated_payment",
        _foreign_ref,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_pay_verify(
            payload=reseller_api.PayVerifyRequest(reference="ref-x"),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400


def _reseller_with_customer(db_session):
    from app.models.subscriber import Reseller, Subscriber, UserType

    reseller = Reseller(name="ViewAs Networks")
    db_session.add(reseller)
    db_session.commit()
    actor = Subscriber(
        first_name="Acting",
        last_name="Reseller",
        email="actor.viewas@example.com",
        user_type=UserType.reseller,
    )
    customer = Subscriber(
        first_name="Jane",
        last_name="Customer",
        email="jane.viewas@example.com",
        reseller_id=reseller.id,
    )
    db_session.add_all([actor, customer])
    db_session.commit()
    from app.models.subscriber import ResellerUser

    db_session.add(
        ResellerUser(subscriber_id=actor.id, reseller_id=reseller.id, is_active=True)
    )
    db_session.commit()
    return reseller, actor, customer


def test_impersonation_token_is_read_only_and_audited(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    from app.models.audit import AuditEvent
    from app.services import auth_dependencies, reseller_portal

    reseller, actor, customer = _reseller_with_customer(db_session)

    out = reseller_portal.create_customer_impersonation_token(
        db_session,
        str(reseller.id),
        str(customer.id),
        acting_subscriber_id=str(actor.id),
    )
    assert out["account_id"] == str(customer.id)
    assert out["customer_name"] == "Jane Customer"

    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reseller_impersonate")
        .filter(AuditEvent.entity_id == str(customer.id))
        .one()
    )
    assert audit.actor_id == str(actor.id)

    class _Req:
        def __init__(self, method):
            self.method = method
            self.cookies = {}
            self.state = type("S", (), {})()

    # Reads pass and identify the impersonated principal + the actor.
    principal = auth_dependencies.require_user_auth(
        authorization=f"Bearer {out['access_token']}",
        request=_Req("GET"),
        db=db_session,
    )
    assert principal["subscriber_id"] == str(customer.id)
    assert principal["impersonated_by"] == str(actor.id)

    # Any mutation under the impersonation token is rejected at the door.
    with pytest.raises(HTTPException) as exc:
        auth_dependencies.require_user_auth(
            authorization=f"Bearer {out['access_token']}",
            request=_Req("POST"),
            db=db_session,
        )
    assert exc.value.status_code == 403


def test_impersonation_404_for_foreign_account(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    from app.models.subscriber import Subscriber
    from app.services import reseller_portal

    reseller, actor, _ = _reseller_with_customer(db_session)
    stranger = Subscriber(
        first_name="Not",
        last_name="Yours",
        email="stranger.viewas@example.com",
    )
    db_session.add(stranger)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        reseller_portal.create_customer_impersonation_token(
            db_session,
            str(reseller.id),
            str(stranger.id),
            acting_subscriber_id=str(actor.id),
        )
    assert exc.value.status_code == 404


def test_service_request_lifecycle(db_session, monkeypatch):
    from app.models.notification import Notification
    from app.services import reseller_service_requests as svc

    reseller, actor, customer = _reseller_with_customer(db_session)

    # Lead without contact info is rejected.
    with pytest.raises(HTTPException) as exc:
        svc.create_request(
            db_session,
            str(reseller.id),
            subscriber_id=None,
            contact_name=None,
            contact_phone=None,
            contact_email=None,
            address=None,
            latitude=None,
            longitude=None,
            notes=None,
        )
    assert exc.value.status_code == 400

    out = svc.create_request(
        db_session,
        str(reseller.id),
        subscriber_id=str(customer.id),
        contact_name=None,
        contact_phone=None,
        contact_email=None,
        address="12 Fiber Close",
        latitude=6.5,
        longitude=3.4,
        notes="Wants 100mbps",
    )
    assert out["status"] == "new"
    # No mapped plant in the fixture DB -> honest 'unknown'.
    assert out["serviceability"] == "unknown"

    mine = svc.list_for_reseller(db_session, str(reseller.id))
    assert [r["id"] for r in mine] == [out["id"]]

    updated = svc.update_status(
        db_session, out["id"], status="scheduled", admin_notes="Crew on Friday"
    )
    assert updated["status"] == "scheduled"
    notes = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == actor.id)
        .filter(Notification.event_type == "service_request_status")
        .all()
    )
    assert len(notes) == 2  # push + email
    assert "Friday" in notes[0].body

    with pytest.raises(HTTPException) as exc:
        svc.update_status(db_session, out["id"], status="bogus")
    assert exc.value.status_code == 400


def test_service_request_rejects_foreign_customer(db_session):
    from app.models.subscriber import Subscriber
    from app.services import reseller_service_requests as svc

    reseller, _, _ = _reseller_with_customer(db_session)
    stranger = Subscriber(
        first_name="Other", last_name="Person", email="other.sr@example.com"
    )
    db_session.add(stranger)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        svc.create_request(
            db_session,
            str(reseller.id),
            subscriber_id=str(stranger.id),
            contact_name=None,
            contact_phone=None,
            contact_email=None,
            address=None,
            latitude=None,
            longitude=None,
            notes=None,
        )
    assert exc.value.status_code == 404


def test_serviceability_distance_flag(db_session):
    from app.models.network import FdhCabinet
    from app.models.service_request import Serviceability
    from app.services import reseller_service_requests as svc

    db_session.add(
        FdhCabinet(
            name="FDH-1", code="FDH1", latitude=6.50, longitude=3.40, is_active=True
        )
    )
    db_session.commit()

    near, near_km = svc.check_serviceability(db_session, 6.501, 3.401)
    assert near == Serviceability.serviceable and near_km is not None

    far, far_km = svc.check_serviceability(db_session, 7.5, 4.4)
    assert far == Serviceability.not_serviceable and far_km > 100
