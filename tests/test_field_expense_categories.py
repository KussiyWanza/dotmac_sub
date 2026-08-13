from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.field import expense_requests as expense_api
from app.api.field import router
from app.services.auth_dependencies import require_user_auth
from app.services.backoffice import ExpenseCategoryView
from app.services.field.expense_categories import ExpenseCategoryQueryError
from app.services.integrations.erp_capability import ErpCapabilityClient


def test_capability_normalizes_expense_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ErpCapabilityClient(object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        client,
        "_execute",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "code": "transport",
                    "name": "Transport",
                    "requires_receipt": True,
                    "max_amount_per_claim": "25000.00",
                }
            ]
        },
    )

    assert client.get_expense_categories() == (
        ExpenseCategoryView(
            category_code="transport",
            category_name="Transport",
            requires_receipt=True,
            max_amount_per_claim=Decimal("25000.00"),
        ),
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = object
    app.dependency_overrides[require_user_auth] = lambda: {"principal_id": "test"}
    return TestClient(app)


def test_category_api_preserves_authoritative_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(expense_api, "list_expense_categories", lambda *_args: ())

    response = _client().get("/api/v1/field/expense-requests/categories")

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0, "limit": 0, "offset": 0}


def test_category_api_maps_erp_unavailability_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object) -> tuple[ExpenseCategoryView, ...]:
        raise ExpenseCategoryQueryError(
            code="operations.expense_categories.erp_unavailable",
            message="Expense categories are temporarily unavailable.",
        )

    monkeypatch.setattr(expense_api, "list_expense_categories", unavailable)

    response = _client().get("/api/v1/field/expense-requests/categories")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "operations.expense_categories.erp_unavailable"
    )
