from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.integrations import router
from app.db import get_db
from app.services.integrations import inbox as integration_inbox
from tests.integration_platform_helpers import (
    enable_capability,
    enable_payment_provider,
)


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: {"sub": "integration-admin"}
    return TestClient(app)


def test_operator_api_manages_additive_installation_lifecycle(db_session) -> None:
    client = _client(db_session)
    name = f"WhatsApp API {uuid4().hex}"

    created = client.post(
        "/api/v1/integrations/installations",
        json={
            "connector_key": "whatsapp",
            "name": name,
            "environment": "sandbox",
        },
    )
    assert created.status_code == 201, created.text
    installation = created.json()
    installation_id = installation["id"]
    assert installation["connector_version"] == "1.1.0"
    assert installation["created_by"] == "integration-admin"
    assert installation["state"] == "draft"

    revision = client.post(
        f"/api/v1/integrations/installations/{installation_id}/config-revisions",
        json={
            "config": {"provider": "meta_cloud_api"},
            "secret_refs": {
                "service_credentials": "bao://secret/integrations/whatsapp#token"
            },
        },
    )
    assert revision.status_code == 201, revision.text
    assert revision.json()["revision"] == 1

    binding = client.put(
        f"/api/v1/integrations/installations/{installation_id}"
        "/capabilities/messaging.send.v1",
        json={"scope": {"audience": "customer"}, "policy": {}},
    )
    assert binding.status_code == 200, binding.text
    assert binding.json()["state"] == "disabled"

    validated = client.post(
        f"/api/v1/integrations/installations/{installation_id}/validate-static"
    )
    assert validated.status_code == 200, validated.text
    assert validated.json() == {"valid": True, "error_codes": [], "details": {}}

    listed = client.get(
        "/api/v1/integrations/installations",
        params={"connector_key": "whatsapp", "state": "disabled"},
    )
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == installation_id for item in listed.json())

    quarantined = client.post(
        f"/api/v1/integrations/installations/{installation_id}/quarantine",
        json={"reason": "credential review"},
    )
    assert quarantined.status_code == 200, quarantined.text
    assert quarantined.json()["state"] == "quarantined"

    retired = client.post(
        f"/api/v1/integrations/installations/{installation_id}/retire",
        json={"reason": "provider removed"},
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["state"] == "retired"

    invalid_secret = client.post(
        f"/api/v1/integrations/installations/{installation_id}/config-revisions",
        json={
            "config": {"provider": "meta_cloud_api"},
            "secret_refs": {"service_credentials": "plaintext"},
        },
    )
    assert invalid_secret.status_code == 400
    assert "retired" in invalid_secret.json()["detail"]


def test_operator_api_rejects_catalogue_only_installation(db_session) -> None:
    response = _client(db_session).post(
        "/api/v1/integrations/installations",
        json={"connector_key": "3cx", "name": f"PBX {uuid4().hex}"},
    )

    assert response.status_code == 400
    assert "no approved executable runtime" in response.json()["detail"]


def test_operator_api_previews_and_adopts_manifest_pin(db_session) -> None:
    bindings = enable_payment_provider(db_session, "paystack")
    installation = bindings["payments.intent.v1"].installation
    installation.connector_version = "1.0.0"
    installation.manifest_digest = (
        "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e"
    )
    installation_id = installation.id
    db_session.commit()
    client = _client(db_session)

    preview_response = client.get(
        f"/api/v1/integrations/installations/{installation_id}/manifest-adoption"
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["pin_state"] == "supported_historical"
    assert preview["adoption_required"] is True
    assert preview["ready"] is True
    assert preview["target_pin"]["connector_version"] == "1.0.1"
    db_session.commit()

    adopted = client.post(
        f"/api/v1/integrations/installations/{installation_id}/manifest-adoption",
        json={
            "expected_installed_pin": preview["installed_pin"],
            "target_pin": preview["target_pin"],
            "reason": "Adopt reviewed Paystack manifest",
            "idempotency_key": "api-paystack-adoption-1",
        },
    )

    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["replayed"] is False
    assert adopted.json()["adopted_pin"] == preview["target_pin"]


def test_operator_api_lists_and_authorizes_inbox_replay(db_session) -> None:
    binding = enable_capability(
        db_session,
        connector_key="whatsapp",
        capability_id="messaging.receive.v1",
        config={"provider": "meta_cloud_api"},
        secret_refs={
            "service_credentials": "env://WHATSAPP_TEST_SERVICE_TOKEN",
            "webhook_signing_secret": "env://WHATSAPP_TEST_SIGNING_SECRET",
        },
    )
    receipt, created = integration_inbox.receive_verified(
        db_session,
        capability_binding_id=binding.id,
        provider_event_id="wamid.operator-replay",
        event_type="messages",
        payload={"message": {"id": "wamid.operator-replay"}},
    )
    assert created is True
    assert integration_inbox.claim_for_processing(receipt) is True
    integration_inbox.mark_failed(
        receipt,
        error_code="downstream_unavailable",
        max_attempts=1,
    )
    db_session.commit()

    client = _client(db_session)
    listed = client.get("/api/v1/integrations/inbox", params={"state": "dead_letter"})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(receipt.id)]

    detail = client.get(f"/api/v1/integrations/inbox/{receipt.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["provider_event_id"] == "wamid.operator-replay"

    replayed = client.post(f"/api/v1/integrations/inbox/{receipt.id}/replay")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["state"] == "verified"

    repeated = client.post(f"/api/v1/integrations/inbox/{receipt.id}/replay")
    assert repeated.status_code == 409
    assert "not replayable" in repeated.json()["detail"]
