from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import text

from app.api import erp_material_webhooks
from app.services.integrations.backoffice_contracts import (
    ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY,
)


class _Request:
    def __init__(self, body: bytes, *, secret: str, delivery_id: str) -> None:
        self._body = body
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.headers = {
            "content-type": "application/json",
            "X-Dotmac-Delivery": delivery_id,
            "X-Dotmac-Signature": f"sha256={signature}",
        }

    async def body(self) -> bytes:
        return self._body


def _payload(request_id: UUID) -> bytes:
    return json.dumps(
        {
            "omni_id": str(request_id),
            "request_id": "erp-request-1",
            "request_number": "MR-0001",
            "old_status": "SUBMITTED",
            "new_status": "ISSUED",
            "items": [{"sequence": 1, "serial_numbers": []}],
        }
    ).encode()


def test_webhook_enters_owner_command_without_adapter_transaction(
    db_session, monkeypatch
) -> None:
    secret = "test-webhook-secret"
    delivery_id = "stable-erp-delivery-1"
    binding_id = uuid4()
    installation_id = uuid4()
    material_request_id = uuid4()
    receipt_id = uuid4()
    claimed = False
    captured_command_ids: list[UUID] = []

    class _Binding:
        capability_id = ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY

        @property
        def installation_id(self) -> UUID:
            # Access after the inbox commit simulates an expired ORM relationship.
            # It would reopen a caller transaction in the old adapter sequence.
            if claimed:
                db_session.execute(text("SELECT 1"))
            return installation_id

    execution = SimpleNamespace(
        binding=_Binding(),
        secret_material={"webhook_signing_secret": secret},
    )
    receipt = SimpleNamespace(id=receipt_id, consequence_json={})

    monkeypatch.setattr(
        erp_material_webhooks,
        "build_execution_context",
        lambda *_args, **_kwargs: execution,
    )

    def receive_and_claim(*_args, **_kwargs):
        nonlocal claimed
        db_session.commit()
        claimed = True
        return receipt, True

    monkeypatch.setattr(
        erp_material_webhooks.integration_inbox,
        "receive_and_claim_verified",
        receive_and_claim,
    )

    def observe(db, command):
        assert not db.in_transaction()
        captured_command_ids.append(command.context.command_id)
        return SimpleNamespace(
            id=material_request_id,
            status=SimpleNamespace(value="issued"),
        )

    monkeypatch.setattr(
        erp_material_webhooks.material_requests,
        "observe_erp_material_status",
        observe,
    )
    monkeypatch.setattr(
        erp_material_webhooks.integration_inbox,
        "get_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    monkeypatch.setattr(
        erp_material_webhooks.integration_inbox,
        "complete_consequence",
        lambda *_args, **_kwargs: None,
    )

    request = _Request(
        _payload(material_request_id), secret=secret, delivery_id=delivery_id
    )
    first = asyncio.run(
        erp_material_webhooks.receive_erp_material_status(
            binding_id, request, db_session
        )
    )
    claimed = False
    second = asyncio.run(
        erp_material_webhooks.receive_erp_material_status(
            binding_id, request, db_session
        )
    )

    assert first.status == second.status == "issued"
    assert captured_command_ids[0] == captured_command_ids[1]


def test_webhook_records_retryable_receipt_when_owner_command_fails(
    db_session, monkeypatch
) -> None:
    secret = "test-webhook-secret"
    binding_id = uuid4()
    installation_id = uuid4()
    material_request_id = uuid4()
    receipt_id = uuid4()
    receipt = SimpleNamespace(id=receipt_id, consequence_json={})
    execution = SimpleNamespace(
        binding=SimpleNamespace(
            capability_id=ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY,
            installation_id=installation_id,
        ),
        secret_material={"webhook_signing_secret": secret},
    )
    failures: list[dict[str, object]] = []

    monkeypatch.setattr(
        erp_material_webhooks,
        "build_execution_context",
        lambda *_args, **_kwargs: execution,
    )

    def receive_and_claim(*_args, **_kwargs):
        db_session.commit()
        return receipt, True

    monkeypatch.setattr(
        erp_material_webhooks.integration_inbox,
        "receive_and_claim_verified",
        receive_and_claim,
    )
    monkeypatch.setattr(
        erp_material_webhooks.material_requests,
        "observe_erp_material_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    def fail_claimed(*_args, **kwargs):
        failures.append(kwargs)

    monkeypatch.setattr(
        erp_material_webhooks.integration_inbox,
        "fail_claimed_consequence",
        fail_claimed,
    )

    request = _Request(
        _payload(material_request_id),
        secret=secret,
        delivery_id="stable-erp-delivery-2",
    )
    try:
        asyncio.run(
            erp_material_webhooks.receive_erp_material_status(
                binding_id, request, db_session
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "failed"
    else:
        raise AssertionError("owner failure was not propagated")

    assert failures == [
        {
            "receipt_id": receipt_id,
            "error_code": "erp_material_status_consequence_failed",
            "error_detail": "RuntimeError",
        }
    ]
