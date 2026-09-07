"""Customer portal Quotes web route: auth gate + registration."""

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.web.customer.quotes import router


def _client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_get_redirects_to_login_when_anonymous(db_session):
    client = _client(db_session)
    with patch(
        "app.web.customer.quotes.get_current_customer_from_request",
        return_value=None,
    ):
        r = client.get("/portal/quotes", follow_redirects=False)
    assert r.status_code == 303
    assert "/portal/auth/login" in r.headers["location"]


def test_route_is_registered():
    from app.web.customer import router as customer_router

    paths = {getattr(route, "path", "") for route in customer_router.routes}
    assert "/portal/quotes" in paths


def test_native_quote_read_still_uses_native_owner(db_session, monkeypatch):
    from unittest.mock import MagicMock

    from app.web.customer import quotes

    native = MagicMock(return_value={"quotes": [], "total": 0, "open": 0})
    historical = MagicMock()
    monkeypatch.setattr(
        quotes.selfserve_service, "native_read_enabled", lambda db: True
    )
    monkeypatch.setattr(
        quotes.selfserve_service.selfserve_quotes, "read_for_subscriber", native
    )
    monkeypatch.setattr(quotes.quotes_mirror, "read_for_subscriber_result", historical)
    payload, state = quotes._quotes(db_session, "test-subscriber")
    assert state == "current"
    assert payload["total"] == 0
    native.assert_called_once()
    historical.assert_not_called()
