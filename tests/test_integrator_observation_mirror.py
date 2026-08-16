"""Parity between an Integrator envelope and what Sub's own receiver recorded.

The harness only earns its place if it *bites*. Most of these tests therefore
construct a disagreement on purpose and assert the exact verdict, because a
comparison that returns "agrees" for everything is indistinguishable from no
comparison at all.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.models.team_inbox import (
    InboxChannelType,
    InboxObservationKind,
    InboxProviderObservation,
)
from app.schemas.integrator_observation import IntegratorObservationEnvelope
from app.services import team_inbox_integrator_mirror as mirror
from app.services.owner_commands import CommandContext
from app.services.team_inbox_observations import (
    InboundMessageObservation,
    InboxProvider,
    RecordProviderObservationCommand,
    normalized_payload,
    observation_fingerprint,
)

OBSERVED_AT = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)


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
        "external_message_id": "wamid.MIRROR1",
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


def _envelope(*, message: dict | None = None, **overrides):
    body = message if message is not None else _message_body()
    payload = {
        "capability_id": "messaging.receive.v1",
        "contract_version": 1,
        "provider": "meta_cloud_api",
        "provider_account_scope": "1234567890",
        "provider_event_id": "wamid.MIRROR1",
        "channel": "whatsapp",
        "observed_at": OBSERVED_AT.isoformat(),
        "payload_fingerprint": _fingerprint(body),
        "scope": {"kind": "inbox", "ref": "support"},
        "message": body,
    }
    payload.update(overrides)
    return IntegratorObservationEnvelope.model_validate(payload)


def _sub_command(**overrides) -> RecordProviderObservationCommand:
    """The command Sub's own WhatsApp receiver builds for the same event."""

    fields = {
        "provider": InboxProvider.meta_cloud_api,
        "provider_account_scope": "1234567890",
        "provider_event_id": "message:wamid.MIRROR1",
        "kind": InboxObservationKind.message,
        "channel_type": InboxChannelType.whatsapp,
        "external_message_id": "wamid.MIRROR1",
        "observed_at": OBSERVED_AT,
        "payload": InboundMessageObservation(
            contact_address="2348030000001",
            body="my link is down",
            contact_name="Ada",
        ),
    }
    fields.update(overrides)
    return RecordProviderObservationCommand(
        context=CommandContext.system(
            actor="transport:meta_cloud_api",
            scope="team-inbox:provider-observation",
            reason="test fixture",
        ),
        **fields,
    )


def _record(db_session, command: RecordProviderObservationCommand):
    """Insert the row Sub's receiver would have written, without its side effects."""

    row = InboxProviderObservation(
        provider=command.provider.value,
        provider_account_scope=command.provider_account_scope,
        provider_event_id=command.provider_event_id,
        observation_kind=command.kind.value,
        channel_type=command.channel_type.value,
        external_message_id=command.external_message_id,
        payload_fingerprint=observation_fingerprint(command),
        normalized_payload=normalized_payload(command.payload),
        observed_at=command.observed_at,
        recorded_at=datetime.now(UTC),
        processing_status="processed",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_identical_producers_agree(db_session):
    _record(db_session, _sub_command())
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_AGREES
    assert report.agrees is True
    assert report.blocking_reasons == ()
    assert report.disagreements == ()


def test_nothing_recorded_is_not_agreement(db_session):
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_NO_COUNTERPART
    assert report.blocking_reasons == (mirror.BLOCKING_NO_COUNTERPART,)
    assert report.counterpart_identity is None


def test_a_differing_field_is_named(db_session):
    _record(
        db_session,
        _sub_command(
            payload=InboundMessageObservation(
                contact_address="2348030000001",
                body="my link is down",
                contact_name="Adaeze",
            )
        ),
    )
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    # Same identity, different content: the observation owner would call this a
    # collision, so the harness must too rather than filing it as a soft diff.
    assert report.verdict == mirror.VERDICT_COLLISION
    assert report.blocking_reasons == (mirror.BLOCKING_COLLISION,)
    fields = {item.field for item in report.disagreements}
    assert "payload_fingerprint" in fields
    assert "normalized_payload.contact_name" in fields


def test_a_disagreement_never_leaks_message_content(db_session):
    _record(
        db_session,
        _sub_command(
            payload=InboundMessageObservation(
                contact_address="2348030000001",
                body="a customer's actual private message",
                contact_name="Ada",
            )
        ),
    )
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    rendered = json.dumps(report.as_dict())
    assert "a customer's actual private message" not in rendered
    assert "my link is down" not in rendered
    assert "2348030000001" not in rendered


def test_identity_shape_mismatch_is_found_and_is_blocking(db_session):
    # THE finding. Sub recorded the same upstream event under a different
    # identity — here without the `message:` prefix. The naive lookup misses
    # it entirely; the harness must not, because at cutover this is one
    # customer message becoming two.
    _record(db_session, _sub_command(provider_event_id="wamid.MIRROR1"))
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_IDENTITY_SHAPE_MISMATCH
    assert mirror.BLOCKING_IDENTITY_SHAPE in report.blocking_reasons
    assert report.counterpart_identity == (
        "meta_cloud_api",
        "1234567890",
        "wamid.MIRROR1",
    )
    assert report.identity == (
        "meta_cloud_api",
        "1234567890",
        "message:wamid.MIRROR1",
    )
    named = {item.field for item in report.disagreements}
    assert "provider_event_id" in named


def test_an_identity_shape_mismatch_reports_field_drift_too(db_session):
    # A clean field list under a mismatched identity would read as reassurance.
    _record(
        db_session,
        _sub_command(
            provider_event_id="wamid.MIRROR1",
            channel_type=InboxChannelType.chat_widget,
        ),
    )
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_IDENTITY_SHAPE_MISMATCH
    assert report.blocking_reasons == (
        mirror.BLOCKING_IDENTITY_SHAPE,
        mirror.BLOCKING_FIELD_DISAGREEMENT,
    )


def test_a_differing_account_scope_is_an_identity_finding(db_session):
    _record(db_session, _sub_command(provider_account_scope="0987654321"))
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_IDENTITY_SHAPE_MISMATCH
    named = {item.field for item in report.disagreements}
    assert "provider_account_scope" in named


def test_identity_values_are_shown_because_an_operator_cannot_act_without_them(
    db_session,
):
    _record(db_session, _sub_command(provider_event_id="wamid.MIRROR1"))
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    disagreement = next(
        item for item in report.disagreements if item.field == "provider_event_id"
    )
    assert disagreement.integrator == "message:wamid.MIRROR1"
    assert disagreement.sub == "wamid.MIRROR1"


def test_the_mirror_writes_nothing(db_session):
    _record(db_session, _sub_command())
    before = db_session.query(InboxProviderObservation).count()
    row = db_session.query(InboxProviderObservation).one()
    before_status, before_processed = row.processing_status, row.processed_at

    mirror.compare_envelope(db_session, envelope=_envelope())

    db_session.expire_all()
    assert db_session.query(InboxProviderObservation).count() == before
    after = db_session.query(InboxProviderObservation).one()
    assert after.processing_status == before_status
    assert after.processed_at == before_processed


def test_an_empty_population_is_never_cutover_safe(db_session):
    report = mirror.compare_population(db_session, envelopes=())
    assert report.compared == 0
    assert report.is_cutover_safe is False


def test_a_clean_population_is_cutover_safe(db_session):
    _record(db_session, _sub_command())
    report = mirror.compare_population(db_session, envelopes=(_envelope(),))
    assert report.compared == 1
    assert report.agreeing == 1
    assert report.blocking_reason_counts == {}
    assert report.is_cutover_safe is True


def test_one_bad_event_blocks_a_whole_population(db_session):
    _record(db_session, _sub_command())
    other = _message_body(external_message_id="wamid.MIRROR2")
    stray = _envelope(
        message=other,
        provider_event_id="wamid.MIRROR2",
        payload_fingerprint=_fingerprint(other),
    )
    report = mirror.compare_population(db_session, envelopes=(_envelope(), stray))
    assert report.compared == 2
    assert report.agreeing == 1
    assert report.is_cutover_safe is False
    assert mirror.BLOCKING_NO_COUNTERPART in report.blocking_reason_counts


def test_the_population_report_is_deterministic(db_session):
    _record(db_session, _sub_command())
    envelopes = (_envelope(), _envelope())
    first = mirror.compare_population(db_session, envelopes=envelopes)
    second = mirror.compare_population(db_session, envelopes=envelopes)
    assert first.as_dict() == second.as_dict()


def test_the_harness_uses_the_owners_own_fingerprint_rule(db_session):
    # If the harness ever recomputed its own equivalent of the domain
    # fingerprint, the two definitions would drift and the harness would start
    # reporting agreement the observation owner would refuse.
    command = _sub_command()
    row = _record(db_session, command)
    assert row.payload_fingerprint == observation_fingerprint(command)
    report = mirror.compare_envelope(db_session, envelope=_envelope())
    assert report.verdict == mirror.VERDICT_AGREES


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (mirror.VERDICT_AGREES, ()),
        (mirror.VERDICT_COLLISION, (mirror.BLOCKING_COLLISION,)),
        (mirror.VERDICT_NO_COUNTERPART, (mirror.BLOCKING_NO_COUNTERPART,)),
        (mirror.VERDICT_FIELD_DISAGREEMENT, (mirror.BLOCKING_FIELD_DISAGREEMENT,)),
    ],
)
def test_every_verdict_has_a_stable_blocking_reason(verdict, expected):
    report = mirror.ObservationMirrorReport(
        verdict=verdict,
        identity=("p", "s", "e"),
        counterpart_identity=None,
        disagreements=(),
    )
    assert report.blocking_reasons == expected
