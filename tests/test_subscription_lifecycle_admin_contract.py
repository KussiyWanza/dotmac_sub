from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic_core import PydanticUndefined

from app.services import web_catalog_subscriptions
from app.services.subscription_lifecycle import SubscriptionCommandKind
from app.web.admin import catalog as catalog_routes

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_admin_lifecycle_endpoint_requires_review_and_idempotency() -> None:
    signature = inspect.signature(
        catalog_routes.catalog_subscription_execute_lifecycle_command
    )

    expected_head = signature.parameters["expected_head"].default
    idempotency_key = signature.parameters["idempotency_key"].default
    assert expected_head.default is PydanticUndefined
    assert idempotency_key.default is PydanticUndefined
    assert idempotency_key.alias == "Idempotency-Key"


def test_narrow_lifecycle_permissions_only_authorize_their_command(
    monkeypatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_id": "operator-1"})
    )
    granted = {"subscription:activate"}
    monkeypatch.setattr(
        catalog_routes,
        "has_permission",
        lambda auth, db, permission: permission in granted,
    )

    catalog_routes._assert_lifecycle_command_permission(
        request, object(), SubscriptionCommandKind.activate
    )
    catalog_routes._assert_lifecycle_command_permission(
        request, object(), SubscriptionCommandKind.restore
    )
    with pytest.raises(HTTPException) as suspended:
        catalog_routes._assert_lifecycle_command_permission(
            request, object(), SubscriptionCommandKind.suspend
        )
    with pytest.raises(HTTPException) as canceled:
        catalog_routes._assert_lifecycle_command_permission(
            request, object(), SubscriptionCommandKind.cancel
        )

    assert suspended.value.status_code == 403
    assert canceled.value.status_code == 403


def test_catalog_write_authorizes_every_lifecycle_command(monkeypatch) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_id": "operator-2"})
    )
    monkeypatch.setattr(
        catalog_routes,
        "has_permission",
        lambda auth, db, permission: permission == "catalog:write",
    )

    for kind in SubscriptionCommandKind:
        catalog_routes._assert_lifecycle_command_permission(request, object(), kind)


def test_lifecycle_preview_accepts_reader_or_exact_action_permission(
    monkeypatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_id": "operator-preview"})
    )
    granted = {"subscription:activate"}
    monkeypatch.setattr(
        catalog_routes,
        "has_permission",
        lambda auth, db, permission: permission in granted,
    )

    catalog_routes._assert_lifecycle_preview_permission(
        request,
        object(),
        SubscriptionCommandKind.restore,
    )
    with pytest.raises(HTTPException) as suspended:
        catalog_routes._assert_lifecycle_preview_permission(
            request,
            object(),
            SubscriptionCommandKind.suspend,
        )
    assert suspended.value.status_code == 403

    granted.clear()
    granted.add("catalog:read")
    for kind in SubscriptionCommandKind:
        catalog_routes._assert_lifecycle_preview_permission(request, object(), kind)


def test_legacy_bulk_adapters_delegate_without_direct_lifecycle_writes() -> None:
    for adapter in (
        web_catalog_subscriptions.bulk_update_status,
        web_catalog_subscriptions.bulk_change_plan,
    ):
        source = inspect.getsource(adapter)
        assert "execute_subscription_command_batch" in source
        assert "catalog_service.subscriptions.update" not in source
        assert "subscription_change_requests.schedule" not in source


def test_generic_edit_form_does_not_offer_parallel_lifecycle_mutations() -> None:
    template = (
        PROJECT_ROOT / "templates/admin/catalog/subscription_form.html"
    ).read_text(encoding="utf-8")

    assert (
        '{% if is_new %}\n                        <select name="offer_id"' in template
    )
    assert (
        '<input type="hidden" name="offer_id" id="offer_id" '
        'value="{{ subscription.offer_id }}">' in template
    )
    assert "Use <strong>Change Plan</strong>" in template
    assert '<select name="status" id="status"' in template
    for value, label in (
        ("pending", "Pending"),
        ("active", "Active"),
        ("suspended", "Suspended"),
        ("disabled", "Disabled"),
        ("canceled", "Canceled"),
    ):
        assert f'<option value="{value}"' in template
        assert f">{label}</option>" in template
    assert 'name="status" value="{{ subscription.status }}"' in template
    assert (
        'id="billing_mode" x-model="billingMode" '
        "{% if subscription.id %}disabled" in template
    )
    for field in ("start_at", "next_billing_at", "end_at"):
        assert f'id="{field}" readonly aria-readonly="true"' in template
    assert 'id="canceled_at" readonly' in template
    assert 'id="cancel_reason" readonly' in template
    assert "{% if subscription.id %}\n        {# Lifecycle facts" in template
    assert "Activate after creation" not in template
    assert "Set status to Active instead of Pending" not in template
    assert "bg-violet-600" not in template
