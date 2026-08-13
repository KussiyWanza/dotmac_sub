from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.field.work_order_compat import resolve_work_order_id
from app.schemas.field import (
    FieldExpenseRequestCreate,
    FieldMaterialRequestCreate,
)


def _expense_payload() -> dict:
    return {
        "purpose": "Transport",
        "currency": "NGN",
        "items": [
            {
                "category_code": "transport",
                "description": "Taxi",
                "amount": "1000.00",
            }
        ],
    }


def _material_payload() -> dict:
    return {
        "priority": "medium",
        "source_warehouse_code": "WH-LAGOS",
        "items": [
            {
                "item_id": "11111111-1111-1111-1111-111111111111",
                "quantity": 1,
            }
        ],
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (FieldExpenseRequestCreate, _expense_payload()),
        (FieldMaterialRequestCreate, _material_payload()),
    ),
)
def test_json_inputs_accept_legacy_work_order_name(model, payload) -> None:
    parsed = model.model_validate({**payload, "crm_work_order_id": "wo-legacy"})

    assert parsed.work_order_id == "wo-legacy"
    assert parsed.model_dump()["work_order_id"] == "wo-legacy"
    assert "crm_work_order_id" not in parsed.model_dump()


@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (FieldExpenseRequestCreate, _expense_payload()),
        (FieldMaterialRequestCreate, _material_payload()),
    ),
)
def test_json_inputs_accept_matching_aliases_and_reject_conflicts(
    model, payload
) -> None:
    matching = model.model_validate(
        {
            **payload,
            "work_order_id": "wo-same",
            "crm_work_order_id": "wo-same",
        }
    )
    assert matching.work_order_id == "wo-same"

    with pytest.raises(ValidationError, match="must identify the same work order"):
        model.model_validate(
            {
                **payload,
                "work_order_id": "wo-new",
                "crm_work_order_id": "wo-old",
            }
        )


def test_query_and_form_alias_resolution() -> None:
    assert (
        resolve_work_order_id(work_order_id=None, crm_work_order_id="wo-legacy")
        == "wo-legacy"
    )
    assert (
        resolve_work_order_id(work_order_id="wo-same", crm_work_order_id="wo-same")
        == "wo-same"
    )
    with pytest.raises(HTTPException) as exc_info:
        resolve_work_order_id(work_order_id="wo-new", crm_work_order_id="wo-legacy")
    assert exc_info.value.status_code == 422
