"""The eight properties Sub's `messaging.receive.v1` port has to satisfy.

Each is a property, not a smoke test. Together they say: an authenticated
Integrator can record an inbound message exactly once, cannot decide where it
lands, cannot enumerate what Sub accepts, and cannot make a collision look like
a duplicate.

The fast lane runs on SQLite and is explicitly not deployed-schema acceptance
(`AGENTS.md`, "Database-test authority"). What it does prove is the port's
logic: identity construction, refusal shapes, delegation, and the row counts
that go with each.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import integrator_observations
from app.db import get_db
from app.models.auth import ApiKey
from app.models.integration_platform import IntegrationInbox
from app.models.team_inbox import (
    InboxChannelType,
    InboxMessage,
    InboxProviderObservation,
)
from app.schemas.integrator_observation import IntegratorObservationEnvelope
from app.services.auth import hash_api_key
from app.services.integrations.connectors.integrator_http import (
    INTEGRATOR_CONNECTOR_KEY,
    INTEGRATOR_RECEIVE_CAPABILITY,
)
from tests.integration_platform_helpers import enable_capability

WRITE_TOKEN = "integrator-write-token"
MIRROR_TOKEN = "integrator-mirror-token"


def _fingerprint(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _message_body(**overrides) -> dict:
    body = {
        "contact_address": "2348030000001",
        "body": "my link is down",
        "contact_name": "Ada",
        "subject": None,
        "external_message_id": "wamid.TEST0001",
        "external_thread_id": None,
        "provider_account_id": None,
        "external_account_id": None,
        "page_id": None,
        "instagram_account_id": None,
        "surface": None,
        "permalink_url": None,
        "media_url": None,
        "contact_profile": None,
        "attachments": [],
    }
    body.update(overrides)
    return body


def _envelope(*, message: dict | None = None, **overrides) -> dict:
    body = message if message is not None else _message_body()
    envelope = {
        "capability_id": INTEGRATOR_RECEIVE_CAPABILITY,
        "contract_version": 1,
        "provider": "meta_cloud_api",
        "provider_account_scope": "1234567890",
        "provider_event_id": "wamid.TEST0001",
        "channel": "whatsapp",
        "observed_at": datetime(2026, 8, 16, 9, 0, tzinfo=UTC).isoformat(),
        "payload_fingerprint": _fingerprint(body),
        "scope": {"kind": "inbox", "ref": "support"},
        "message": body,
    }
    envelope.update(overrides)
    return envelope


@pytest.fixture()
def binding(db_session):
    return enable_capability(
        db_session,
        connector_key=INTEGRATOR_CONNECTOR_KEY,
        capability_id=INTEGRATOR_RECEIVE_CAPABILITY,
        config={},
        secret_refs={},
    )


@pytest.fixture()
def keys(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            ApiKey(
                label="integrator-write",
                key_hash=hash_api_key(WRITE_TOKEN),
                scopes=[integrator_observations.INTEGRATOR_OBSERVATION_SCOPE],
                is_active=True,
                expires_at=now + timedelta(days=1),
            ),
            ApiKey(
                label="integrator-mirror",
                key_hash=hash_api_key(MIRROR_TOKEN),
                scopes=[integrator_observations.INTEGRATOR_MIRROR_SCOPE],
                is_active=True,
                expires_at=now + timedelta(days=1),
            ),
            ApiKey(
                label="integrator-revoked",
                key_hash=hash_api_key("revoked-token"),
                scopes=[integrator_observations.INTEGRATOR_OBSERVATION_SCOPE],
                is_active=True,
                revoked_at=now - timedelta(minutes=1),
            ),
            ApiKey(
                label="integrator-expired",
                key_hash=hash_api_key("expired-token"),
                scopes=[integrator_observations.INTEGRATOR_OBSERVATION_SCOPE],
                is_active=True,
                expires_at=now - timedelta(minutes=1),
            ),
            ApiKey(
                label="integrator-unscoped",
                key_hash=hash_api_key("unscoped-token"),
                scopes=[],
                is_active=True,
                expires_at=now + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()


@pytest.fixture()
def client(db_session, keys):
    app = FastAPI()
    app.include_router(integrator_observations.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def _post(client, binding, envelope, *, token=WRITE_TOKEN, path="") -> object:
    return client.post(
        f"/api/v1/integration/observations/{binding.id}{path}",
        json=envelope,
        headers={"X-Api-Key": token} if token else {},
    )


def _counts(db_session) -> tuple[int, int, int]:
    return (
        db_session.query(InboxProviderObservation).count(),
        db_session.query(IntegrationInbox).count(),
        db_session.query(InboxMessage).count(),
    )


# 1 — unauthenticated and wrongly-scoped calls change no row count.


@pytest.mark.parametrize(
    "token",
    [None, "not-a-real-key", "revoked-token", "expired-token", "unscoped-token"],
)
def test_rejected_credentials_change_no_row_count(client, db_session, binding, token):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(), token=token)
    assert response.status_code in (401, 403)
    assert _counts(db_session) == before


# 2 — a provider signature is not accepted in place of Integrator auth.
#
# The sensitivity proof for "authenticate the Integrator, not the provider":
# a request carrying a perfectly well-formed Meta HMAC and no Integrator
# credential must be refused. If this ever passes with a 2xx, the port has
# grown a second, wrong authentication path.


def test_a_provider_signature_is_not_integrator_authentication(
    client, db_session, binding
):
    before = _counts(db_session)
    body = json.dumps(_envelope()).encode()
    response = client.post(
        f"/api/v1/integration/observations/{binding.id}",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": "sha256="
            + hashlib.sha256(b"any-app-secret" + body).hexdigest(),
        },
    )
    assert response.status_code == 401
    assert _counts(db_session) == before


# 3 — replay is one observation.


def test_replay_records_one_observation_and_one_consequence(
    client, db_session, binding
):
    envelope = _envelope()
    first = _post(client, binding, envelope)
    assert first.status_code == 200, first.text
    second = _post(client, binding, envelope)
    assert second.status_code == 200, second.text

    assert db_session.query(InboxProviderObservation).count() == 1
    assert db_session.query(InboxMessage).count() == 1
    assert second.json()["replayed"] is True
    assert second.json()["observation_id"] == first.json()["observation_id"]


# 4 — collision escalates, and the original content survives byte-identical.


def test_collision_escalates_and_preserves_the_original_payload(
    client, db_session, binding
):
    assert _post(client, binding, _envelope()).status_code == 200
    original = db_session.query(InboxProviderObservation).one()
    before = dict(original.normalized_payload)
    before_fingerprint = original.payload_fingerprint

    changed = _message_body(body="a completely different message")
    response = _post(
        client,
        binding,
        _envelope(message=changed, payload_fingerprint=_fingerprint(changed)),
    )
    assert response.status_code == 409

    db_session.expire_all()
    after = db_session.query(InboxProviderObservation).one()
    assert dict(after.normalized_payload) == before
    assert after.payload_fingerprint == before_fingerprint


# 5 — the port assigns no authoritative status.
#
# Asserted by OWNER rather than by table: the port's own writes are the receipt
# and the observation, and every Team Inbox mutation must be attributable to
# the processing/receive owners. A future direct write from the adapter would
# show up here as a status the port set itself.


def test_the_port_writes_no_authoritative_team_inbox_status(
    client, db_session, binding
):
    assert _post(client, binding, _envelope()).status_code == 200
    observation = db_session.query(InboxProviderObservation).one()
    # processed_at / conversation_id / message_id are the processing owner's to
    # advance. The port never writes them, so their presence here is proof the
    # delegation happened rather than proof the port did the work.
    assert observation.processing_status == "processed"
    assert observation.conversation_id is not None
    assert observation.message_id is not None

    # The transport receipt names what the port itself settled: the observation
    # and its outcome, and nothing about a conversation, team or ticket. If a
    # routing or handoff key ever appears here, the port started deciding.
    receipt = db_session.query(IntegrationInbox).one()
    assert set(receipt.consequence_json) == {
        "observation_id",
        "outcome",
        "processing_status",
        "installation_id",
    }
    # The static half of this property — that the port imports no decision
    # owner and issues no statement of its own — is
    # tests/architecture/test_integrator_port_boundary.py.


# 6 — envelope `scope` cannot select a team.


def test_envelope_scope_does_not_change_routing(client, db_session, binding):
    first = _post(client, binding, _envelope())
    assert first.status_code == 200
    baseline = db_session.query(InboxProviderObservation).one()
    baseline_conversation = baseline.conversation_id

    other = _message_body(external_message_id="wamid.TEST0002")
    response = _post(
        client,
        binding,
        _envelope(
            message=other,
            provider_event_id="wamid.TEST0002",
            payload_fingerprint=_fingerprint(other),
            scope={"kind": "inbox", "ref": "a-team-that-must-not-be-chosen"},
        ),
    )
    assert response.status_code == 200
    db_session.expire_all()
    second = (
        db_session.query(InboxProviderObservation)
        .filter(InboxProviderObservation.provider_event_id == "message:wamid.TEST0002")
        .one()
    )
    # Same contact, same channel, same team decision — the differing scope
    # changed nothing, because Sub's routing owner never read it.
    assert second.conversation_id == baseline_conversation
    assert second.channel_type == InboxChannelType.whatsapp.value


# 7 — an undeployed contract version refuses and writes nothing.


def test_an_undeployed_contract_version_refuses(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(contract_version=2))
    assert response.status_code == 409
    assert _counts(db_session) == before


# 8 — unknown capability is 404, not 403.
#
# An authenticated caller must not be able to enumerate what Sub accepts, so
# "I do not accept that" and "no such binding" answer identically.


def test_unknown_capability_is_not_found_not_forbidden(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(capability_id="billing.receive.v1"))
    assert response.status_code == 404
    assert _counts(db_session) == before


# Normalization properties the eight above depend on.


def test_a_mangled_body_is_refused_by_its_own_fingerprint(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(payload_fingerprint="0" * 64))
    assert response.status_code == 400
    assert _counts(db_session) == before


def test_a_media_only_message_keeps_empty_text_and_typed_location(
    client, db_session, binding
):
    message = _message_body(
        body="",
        attachments=[
            {
                "asset_type": "location",
                "location": {
                    "latitude": 9.0765,
                    "longitude": 7.3986,
                    "name": "Customer location",
                    "address": "Abuja, Nigeria",
                },
            }
        ],
    )

    response = _post(
        client,
        binding,
        _envelope(message=message, payload_fingerprint=_fingerprint(message)),
    )

    assert response.status_code == 200, response.text
    observation = db_session.query(InboxProviderObservation).one()
    assert observation.normalized_payload["body"] == ""
    assert observation.normalized_payload["attachments"][0]["location"] == {
        "latitude": 9.0765,
        "longitude": 7.3986,
        "name": "Customer location",
        "address": "Abuja, Nigeria",
    }
    recorded_message = db_session.query(InboxMessage).one()
    assert recorded_message.body == ""
    assert recorded_message.metadata_["ai_intake_status"] == "skipped"
    assert recorded_message.metadata_["ai_intake_reason"] == "no_text_content"
    assert recorded_message.metadata_["attachments"][0]["location"]["latitude"] == (
        9.0765
    )


def test_a_message_with_neither_text_nor_attachment_is_refused(
    client, db_session, binding
):
    before = _counts(db_session)
    message = _message_body(body="", attachments=[])

    response = _post(
        client,
        binding,
        _envelope(message=message, payload_fingerprint=_fingerprint(message)),
    )

    assert response.status_code == 422
    assert _counts(db_session) == before

    with pytest.raises(ValidationError):
        IntegratorObservationEnvelope.model_validate(
            _envelope(message=message, payload_fingerprint=_fingerprint(message))
        )


def test_an_out_of_range_location_is_refused(client, db_session, binding):
    before = _counts(db_session)
    message = _message_body(
        body="",
        attachments=[
            {
                "asset_type": "location",
                "location": {"latitude": 91.0, "longitude": 7.3986},
            }
        ],
    )

    response = _post(
        client,
        binding,
        _envelope(message=message, payload_fingerprint=_fingerprint(message)),
    )

    assert response.status_code == 422
    assert _counts(db_session) == before


def test_a_channel_its_provider_does_not_carry_is_refused(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(channel="instagram_dm"))
    assert response.status_code == 400
    assert _counts(db_session) == before


@pytest.mark.parametrize("channel", ["note", "field_job", "website_fiber"])
def test_internal_channels_can_never_be_asserted_by_the_caller(
    client, db_session, binding, channel
):
    # `note` and `field_job` have no external transport at all and
    # `website_fiber` carries a different observation type under a different
    # capability. An authenticated caller must not be able to mint one.
    before = _counts(db_session)
    response = _post(client, binding, _envelope(channel=channel))
    assert response.status_code == 400
    assert _counts(db_session) == before


def test_the_identity_matches_subs_own_receiver_convention(client, db_session, binding):
    # The whole overlap window rests on this: Sub's WhatsApp webhook records
    # `message:{wamid}`, so the Integrator must too, or one upstream event
    # becomes two observations at cutover.
    assert _post(client, binding, _envelope()).status_code == 200
    observation = db_session.query(InboxProviderObservation).one()
    assert observation.provider == "meta_cloud_api"
    assert observation.provider_event_id == "message:wamid.TEST0001"
    assert observation.provider_account_scope == "1234567890"


def test_the_shadow_route_writes_nothing(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(), token=MIRROR_TOKEN, path="/mirror")
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "no_counterpart"
    assert _counts(db_session) == before


def test_the_mirror_scope_cannot_write(client, db_session, binding):
    before = _counts(db_session)
    response = _post(client, binding, _envelope(), token=MIRROR_TOKEN)
    assert response.status_code in (401, 403)
    assert _counts(db_session) == before


def test_the_write_scope_is_not_accepted_on_the_shadow_route(
    client, db_session, binding
):
    # Deliberately separate scopes, so a shadow credential can never become a
    # writer and a writer is never silently used to gather "shadow" evidence.
    response = _post(client, binding, _envelope(), token=WRITE_TOKEN, path="/mirror")
    assert response.status_code in (401, 403)
