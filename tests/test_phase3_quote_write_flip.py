"""Phase 3 §4.3 — quote-request write surfaces behind ``quotes_native_write_enabled``.

OFF (default) routes to the retired ``quotes_mirror.request_quote`` owner,
which refuses without contacting CRM; ON creates the quote in sub's native
``quotes`` table via ``SelfServeQuotes.request_quote`` and returns the §2.5
portal payload. The native path needs no CRM linkage, so native-only customers
can request quotes after cutover.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.api import me as me_api
from app.api import reseller as reseller_api
from app.models.sales import Quote
from app.models.subscriber import Subscriber
from app.schemas.portal import QuoteRequestCreate
from app.services.sales import selfserve as selfserve_service

_FAP = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")
_PIN = {
    "latitude": 9.0765,
    "longitude": 7.3986,
    "address": "12 Mississippi St, Maitama",
}


def _subscriber(db, **kwargs) -> Subscriber:
    sub = Subscriber(
        first_name="C",
        last_name="R",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        **kwargs,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _request(payload=None) -> QuoteRequestCreate:
    return QuoteRequestCreate(**(payload or _PIN))


def test_me_quote_request_flag_off_routes_to_retired_mirror_owner(
    db_session, monkeypatch
):
    sub = _subscriber(db_session)
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(selfserve_service, "native_write_enabled", lambda db: False)
    sent = {}

    def _mirror_request(db, sid, **kw):
        sent["sid"] = sid
        sent.update(kw)
        return {"id": "q-crm-1", "status": "draft"}

    monkeypatch.setattr(me_api.quotes_mirror, "request_quote", _mirror_request)
    out = me_api.my_quote_request(_request(), db=db_session, principal=principal)
    assert out["id"] == "q-crm-1"
    assert sent["sid"] == str(sub.id)
    assert sent["latitude"] == _PIN["latitude"]


def test_me_quote_request_flag_on_creates_native_quote(db_session, monkeypatch):
    """Flag ON: quote lands in sub's own table with the map pin, and the
    response is the §2.5 payload (id = quote UUID, money as strings)."""
    sub = _subscriber(db_session)
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(selfserve_service, "native_write_enabled", lambda db: True)
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        out = me_api.my_quote_request(_request(), db=db_session, principal=principal)

    quote = db_session.get(Quote, uuid.UUID(out["id"]))
    assert quote is not None and quote.subscriber_id == sub.id
    install = (quote.metadata_ or {}).get("install") or {}
    assert install.get("latitude") == _PIN["latitude"]
    assert install.get("address") == _PIN["address"]
    assert isinstance(out.get("deposit_amount"), str)


def test_me_quote_request_native_needs_no_crm_link(db_session, monkeypatch):
    """Revenue regression: a native-only subscriber (no splynx/CRM id) can
    request a quote on the native path — the mirror path structurally 400s
    for them (resolve_crm_subscriber_id → None)."""
    sub = _subscriber(db_session)
    assert getattr(sub, "splynx_customer_id", None) in (None, "")
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(selfserve_service, "native_write_enabled", lambda db: True)
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        out = me_api.my_quote_request(_request(), db=db_session, principal=principal)
    assert out["status"] == "draft"


def test_reseller_quote_request_flag_on_creates_native_quote(db_session, monkeypatch):
    sub = _subscriber(db_session)
    monkeypatch.setattr(selfserve_service, "native_write_enabled", lambda db: True)
    monkeypatch.setattr(reseller_api, "_reseller_id", lambda db, principal: "r-1")
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "_get_customer_account",
        lambda db, rid, aid: sub,
    )
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        out = reseller_api.my_reseller_quote_request(
            str(sub.id),
            reseller_api.ResellerQuoteRequest(**_PIN),
            db=db_session,
            principal={"principal_type": "subscriber"},
        )
    quote = db_session.get(Quote, uuid.UUID(out["id"]))
    assert quote is not None and quote.subscriber_id == sub.id


def test_reseller_quote_request_flag_off_routes_to_retired_mirror_owner(
    db_session, monkeypatch
):
    sub = _subscriber(db_session)
    monkeypatch.setattr(selfserve_service, "native_write_enabled", lambda db: False)
    monkeypatch.setattr(reseller_api, "_reseller_id", lambda db, principal: "r-1")
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "_get_customer_account",
        lambda db, rid, aid: sub,
    )
    monkeypatch.setattr(
        reseller_api.quotes_mirror,
        "request_quote",
        lambda db, sid, **kw: {"id": "q-crm-2", "sid": sid},
    )
    out = reseller_api.my_reseller_quote_request(
        str(sub.id),
        reseller_api.ResellerQuoteRequest(**_PIN),
        db=db_session,
        principal={"principal_type": "subscriber"},
    )
    assert out == {"id": "q-crm-2", "sid": str(sub.id)}
