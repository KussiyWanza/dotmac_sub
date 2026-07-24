"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException

from app.api.crm_webhooks import receive_crm_customer, receive_crm_event, router
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.integration_platform import IntegrationInbox
from app.models.subscriber import Subscriber
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.services import integration as integration_service
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from tests.integration_platform_helpers import enable_crm_inbound

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    """Compatibility-free test scope; the secret lives in the installation."""
    yield


@pytest.fixture(autouse=True)
def _crm_inbound_installation(db_session, monkeypatch):
    inbound = enable_crm_inbound(db_session, monkeypatch, signing_secret=SECRET)
    ticket = installations.bind_capability(
        db_session,
        installation_id=inbound.installation_id,
        capability_id="crm.ticket_observation.v1",
        policy={"default": True},
    )
    installations.validate_static(
        db_session,
        installation_id=inbound.installation_id,
    )
    installations.enable_after_connection_validation(
        db_session,
        installation_id=inbound.installation_id,
        connection_result=ValidationResult(valid=True),
    )
    target = integration_service.integration_targets.create(
        db_session,
        IntegrationTargetCreate(
            name="DotMac CRM",
            target_type="crm",
        ),
    )
    integration_service.integration_jobs.create(
        db_session,
        IntegrationJobCreate(
            target_id=target.id,
            name="Pull CRM Tickets",
            job_type="sync",
            schedule_type="manual",
            capability_binding_id=ticket.id,
        ),
    )


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw

    async def json(self):
        return json.loads(self._raw)


class _RouteResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _run(coro):
    # Drive the async route on a fresh event loop in a dedicated thread so the
    # full-suite run is immune to a running event loop leaked by an earlier test
    # (asyncio.run() otherwise raises "cannot be called from a running event
    # loop"). The test SQLite engine uses check_same_thread=False + StaticPool,
    # so the shared session is safe to use from the worker thread.
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _post(body: dict, event: str, signature: str | None, db=None):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature-256"] = signature
    try:
        payload = _run(receive_crm_event(_FakeRequest(raw, headers), db))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _post_customer(db_session, body: dict, event: str = "customer.accepted"):
    raw = json.dumps(body).encode()
    headers = {
        "X-Webhook-Event": event,
        "X-Webhook-Signature-256": _sign(raw),
        "Content-Type": "application/json",
    }
    try:
        payload = _run(receive_crm_customer(_FakeRequest(raw, headers), db_session))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _post_customer_raw(db_session, raw: bytes, headers: dict[str, str]):
    try:
        payload = _run(receive_crm_customer(_FakeRequest(raw, headers), db_session))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _http_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


def test_valid_ticket_created_enqueues_sync(monkeypatch, db_session):
    from app.services import control_registry

    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "false")
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["args"] == ["abc-123"]


def test_ticket_event_fails_retryably_when_capability_cutover_is_incomplete(
    db_session,
) -> None:
    from app.services import control_registry

    control_registry.update_canonical_feature_controls(
        db_session,
        payload={"crm.ticket_pull": True},
    )
    binding = installations.require_enabled_capability_binding(
        db_session,
        connector_key="dotmac.crm",
        capability_id="crm.ticket_observation.v1",
    )
    installations.disable_capability_binding(
        db_session,
        capability_binding_id=binding.id,
        actor="test:cutover-drift",
    )
    db_session.commit()
    body = {"ticket_id": "abc-unready"}
    raw = json.dumps(body).encode()

    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        response = _post(body, "ticket.created", _sign(raw), db_session)

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to apply CRM ticket observation."
    enqueue.assert_not_called()
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.error_code == "crm_ticket_observation_not_ready"


def test_ticket_event_noop_when_pull_disabled(monkeypatch, db_session):
    """Flip kill switch: crm.ticket_pull off -> 200 ack, nothing enqueued."""
    from app.services import control_registry

    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "true")
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": False}
    )
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "reason": "ticket_observation_disabled",
        "event": "ticket.created",
    }
    enqueue.assert_not_called()


def test_ticket_event_noop_when_pull_setting_missing(monkeypatch, db_session):
    """No env, no DB row -> the control's on_missing default (off) applies,
    matching the scheduler beat entries' default."""
    monkeypatch.delenv("CRM_TICKET_PULL_ENABLED", raising=False)
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ticket_observation_disabled"
    enqueue.assert_not_called()


def test_ticket_branch_gated_by_canonical_control(monkeypatch, db_session):
    from app.services import control_registry

    monkeypatch.delenv("CRM_TICKET_PULL_ENABLED", raising=False)
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )

    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()

    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": False}
    )
    body = {"ticket_id": "abc-456"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ticket_observation_disabled"
    enqueue.assert_not_called()


def test_bad_signature_rejected(db_session):
    body = {"ticket_id": "abc-123"}
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", "sha256=deadbeef", db_session)
    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_missing_signature_rejected(db_session):
    with _with_secret(SECRET):
        resp = _post({"ticket_id": "x"}, "ticket.created", None, db_session)
    assert resp.status_code == 401


def test_disabled_inbound_capability_fails_closed(db_session):
    from app.services.integrations import installations

    binding = installations.require_enabled_capability_binding(
        db_session,
        connector_key="dotmac.crm",
        capability_id="crm.events.receive.v1",
    )
    installations.retire_installation(
        db_session,
        installation_id=binding.installation_id,
        reason="test_disabled",
    )
    db_session.commit()
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(""):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 503


def test_unknown_event_acknowledged_without_enqueue(db_session):
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "invoice.paid", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


def test_missing_ticket_id_ignored(monkeypatch, db_session):
    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "true")
    body = {"title": "no id"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


def test_customer_accepted_is_stored_as_unmatched_observation(db_session):
    body = {
        "crm_person_id": "19b9cf6c-3597-4d12-8950-3b41e88f66b2",
        "crm_project_id": "63b428bf-c663-466c-9ebe-21f7c8c62acd",
        "crm_quote_id": "71454e77-2e06-40c8-a4ec-9158cc3ca367",
        "crm_sales_order_id": "1daefc7e-d918-4c25-ada7-91589e80ba5d",
        "name": "Abdulkadir Aminu Umar",
        "email": "aminuumara@example.com",
        "phone": "+07011115972",
        "address": "12 Test Street",
        "date_of_birth": "1990-05-14",
        "gender": "female",
        "status": "new",
        "metadata": {
            "subscriber_category": "residential",
            # The Inbox preserves provider facts without applying them to Sub.
            "date_of_birth": "1980-01-01",
            "gender": "male",
        },
    }
    with _with_secret(SECRET):
        resp = _post_customer(db_session, body)

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "status": "observed",
        "observation_status": "unmatched",
        "matched_via": ["crm_person_id"],
    }
    assert db_session.query(Subscriber).count() == 0
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "processed"
    assert receipt.payload_json == body


def test_customer_webhook_http_route_is_registered(db_session):
    app = _http_app(db_session)

    assert any(
        getattr(route, "path", None) == "/api/v1/webhooks/crm/customers"
        and "POST" in getattr(route, "methods", set())
        for route in app.routes
    )


def test_customer_accepted_retry_returns_same_observation_without_account(
    db_session,
):
    body = {
        "crm_person_id": "ba6fe627-d9bc-4383-b018-11fb631b44b3",
        "crm_project_id": "9132439f-0a2b-4a3b-a1d2-8f47eb8b3674",
        "name": "Aliyu Hassan",
        "email": "hassanahlee8@example.com",
        "phone": "+09160483890",
        "status": "new",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, body)
        second = _post_customer(db_session, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["observation_status"] == "unmatched"
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(IntegrationInbox).count() == 1


def test_customer_webhook_matches_exact_provenance_without_writing_profile(db_session):
    subscriber = Subscriber(
        first_name="Existing",
        last_name="Identity",
        display_name="Existing Identity",
        email="existing@example.com",
        phone="08012345678",
        city="Abuja",
        metadata_={"crm_person_id": "4cf4d62b-29a0-493e-8a0d-6409a18e8897"},
    )
    db_session.add(subscriber)
    db_session.commit()
    payload = {
        "crm_person_id": "4cf4d62b-29a0-493e-8a0d-6409a18e8897",
        "name": "Changed Customer",
        "email": "changed.customer@example.com",
        "phone": "+09000000004",
        "address": {"city": "Lagos"},
        "date_of_birth": "not-a-date",
        "gender": "not-a-gender",
        "subscriber_category": "business",
        "status": "disabled",
    }

    response = _post_customer(db_session, payload)

    assert response.status_code == 200
    assert response.json()["observation_status"] == "matched"
    assert response.json()["id"] == str(subscriber.id)
    assert response.json()["matched_via"] == ["crm_person_id"]
    db_session.refresh(subscriber)
    assert (subscriber.first_name, subscriber.last_name, subscriber.display_name) == (
        "Existing",
        "Identity",
        "Existing Identity",
    )
    assert subscriber.email == "existing@example.com"
    assert subscriber.phone == "08012345678"
    assert subscriber.city == "Abuja"
    assert subscriber.date_of_birth is None
    assert subscriber.gender.value == "unknown"
    assert subscriber.category.value == "residential"
    assert subscriber.status.value == "active"
    assert db_session.query(AuditEvent).count() == 0


def test_customer_webhook_placeholder_observation_cannot_replace_existing_name(
    db_session,
):
    subscriber = Subscriber(
        first_name="Existing",
        last_name="Identity",
        display_name="Existing Identity",
        email="existing@example.com",
        phone="08012345678",
        metadata_={"crm_person_id": "535e709a-c375-428a-8afe-b67e81c2c45d"},
    )
    db_session.add(subscriber)
    db_session.commit()

    response = _post_customer(
        db_session,
        {
            "crm_person_id": "535e709a-c375-428a-8afe-b67e81c2c45d",
            "first_name": "Customer",
            "last_name": "Customer",
            "display_name": "Customer Customer",
            "email": "remote-placeholder@example.com",
            "date_of_birth": "1993-07-15",
            "gender": "female",
            "status": "active",
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(subscriber.id)
    assert response.json()["observation_status"] == "matched"
    db_session.refresh(subscriber)
    assert (subscriber.first_name, subscriber.last_name, subscriber.display_name) == (
        "Existing",
        "Identity",
        "Existing Identity",
    )
    assert subscriber.email == "existing@example.com"
    assert subscriber.date_of_birth is None
    assert subscriber.gender.value == "unknown"
    assert subscriber.status.value == "active"
    assert db_session.query(AuditEvent).count() == 0


@pytest.mark.parametrize(
    "name",
    ["Customer", "Unknown", "Unknown Customer", "N/A", "Not Specified"],
)
def test_customer_webhook_does_not_create_account_from_placeholder(db_session, name):
    response = _post_customer(
        db_session,
        {
            "crm_person_id": f"placeholder-{name.lower().replace(' ', '-')}",
            "name": name,
            "email": "placeholder@example.com",
            "status": "new",
        },
    )

    assert response.status_code == 200
    assert response.json()["observation_status"] == "unmatched"
    assert db_session.query(Subscriber).count() == 0


def test_customer_webhook_placeholder_observation_is_processed(db_session):
    response = _post_customer(
        db_session,
        {
            "crm_person_id": "placeholder-route-rejection",
            "name": "Unknown Unknown",
            "email": "placeholder-route@example.com",
            "status": "new",
        },
    )

    assert response.status_code == 200
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "processed"
    assert receipt.error_code is None
    assert db_session.query(Subscriber).count() == 0


def test_customer_webhook_missing_identity_fields_preserve_existing_values(db_session):
    subscriber = Subscriber(
        first_name="Existing",
        last_name="Customer",
        email="existing.identity@example.com",
        metadata_={"crm_person_id": "identity-preserve-1"},
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "crm_person_id": "identity-preserve-1",
        "name": "Existing Customer",
        "email": "existing.identity@example.com",
        "date_of_birth": "",
        "gender": None,
        "metadata": {"date_of_birth": "2000-01-01", "gender": "female"},
    }
    with _with_secret(SECRET):
        response = _post_customer(db_session, body)

    assert response.status_code == 200
    db_session.refresh(subscriber)
    assert subscriber.date_of_birth is None
    assert subscriber.gender.value == "unknown"
    assert subscriber.metadata_ == {"crm_person_id": "identity-preserve-1"}


@pytest.mark.parametrize(
    "field,value", [("date_of_birth", "not-a-date"), ("gender", "invalid-gender")]
)
def test_customer_webhook_accepts_invalid_remote_profile_as_observation_only(
    db_session, field, value
):
    subscriber = Subscriber(
        first_name="Safe",
        last_name="Customer",
        email="safe.identity@example.com",
        metadata_={"crm_person_id": "identity-invalid-1"},
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "crm_person_id": "identity-invalid-1",
        "name": "Safe Customer",
        "email": "safe.identity@example.com",
        field: value,
    }
    response = _post_customer(db_session, body)

    assert response.status_code == 200
    assert response.json()["observation_status"] == "matched"
    db_session.refresh(subscriber)
    assert subscriber.date_of_birth is None
    assert subscriber.gender.value == "unknown"


def test_customer_webhook_unmatched_replay_is_idempotent_without_account(
    db_session,
):
    body = {
        "crm_person_id": "identity-replay-1",
        "name": "Replay Customer",
        "email": "replay.identity@example.com",
        "date_of_birth": "1994-04-05",
        "gender": "non_binary",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, body)
        second = _post_customer(db_session, body)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["observation_status"] == "unmatched"
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(IntegrationInbox).count() == 1


def test_customer_webhook_does_not_match_by_name_email_or_phone(db_session):
    subscriber = Subscriber(
        first_name="Normalized",
        last_name="Customer",
        display_name="Normalized Customer",
        email="old.normalized@example.com",
        phone="08012345678",
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "name": "Normalized Customer",
        "email": "new.normalized@example.com",
        "phone": "+2348012345678",
        "status": "active",
    }

    with _with_secret(SECRET):
        response = _post_customer(db_session, body)

    assert response.status_code == 200
    assert response.json()["observation_status"] == "unmatched"
    assert "id" not in response.json()
    assert db_session.query(Subscriber).count() == 1
    db_session.refresh(subscriber)
    assert subscriber.email == "old.normalized@example.com"


def test_shared_project_id_does_not_create_or_merge_customers(db_session):
    project_id = "5e9d2c11-7a44-4b0e-9a3c-2f1d6b8e4c77"
    first_body = {
        "crm_person_id": "11111111-1111-4111-8111-111111111111",
        "crm_project_id": project_id,
        "name": "Person One",
        "email": "person.one@example.com",
        "phone": "+09000000001",
        "status": "new",
    }
    second_body = {
        "crm_person_id": "22222222-2222-4222-8222-222222222222",
        "crm_project_id": project_id,
        "name": "Person Two",
        "email": "person.two@example.com",
        "phone": "+09000000002",
        "status": "new",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, first_body)
        second = _post_customer(db_session, second_body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["observation_status"] == "unmatched"
    assert second.json()["observation_status"] == "unmatched"
    assert db_session.query(Subscriber).count() == 0


def test_customer_webhook_reports_ambiguous_exact_provenance_without_writing(
    db_session,
):
    crm_person_id = "duplicate-provenance"
    db_session.add_all(
        [
            Subscriber(
                first_name="First",
                last_name="Account",
                email="first@example.com",
                metadata_={"crm_person_id": crm_person_id},
            ),
            Subscriber(
                first_name="Second",
                last_name="Account",
                email="second@example.com",
                metadata_={"crm_person_id": crm_person_id},
            ),
        ]
    )
    db_session.commit()

    response = _post_customer(db_session, {"crm_person_id": crm_person_id})

    assert response.status_code == 200
    assert response.json() == {
        "status": "observed",
        "observation_status": "ambiguous",
        "matched_via": ["crm_person_id"],
    }
    assert db_session.query(Subscriber).count() == 2


def test_customer_webhook_rejects_bad_signature(db_session):
    raw = json.dumps({"name": "Bad Sig", "email": "bad@example.com"}).encode()
    with _with_secret(SECRET):
        resp = _post_customer_raw(
            db_session,
            raw,
            {
                "X-Webhook-Event": "customer.accepted",
                "X-Webhook-Signature-256": "sha256=bad",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401


# --- S4a: replay dedup on the previously un-deduped routes ---


def test_ticket_webhook_replay_is_deduped(monkeypatch, db_session):
    """A redelivery with the same delivery id must not enqueue a second pull."""
    from app.services import control_registry

    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )
    body = {"ticket_id": "t-1"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        first = _post(body, "ticket.created", _sign(raw), db_session)
        replay = _post(body, "ticket.created", _sign(raw), db_session)
    assert first.status_code == 200
    assert replay.status_code == 200 and replay.json()["status"] == "queued"
    assert enqueue.call_count == 1
