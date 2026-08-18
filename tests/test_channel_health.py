from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from starlette.exceptions import HTTPException

from app.api.webhook_observation import webhook_observation
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxChannelType, InboxConversation, InboxMessage
from app.services import (
    channel_health,
    channel_health_contracts,
    team_inbox_channel_receive,
)


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _receive(db_session, *, channel: str, wamid: str, when: datetime, team) -> object:
    return team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=channel,
            contact_address=f"{channel}:+2348035550{wamid[-3:]}",
            body="hello",
            external_message_id=wamid,
            fallback_service_team_id=team.id,
            received_at=when,
        ),
    )


def _counter(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_channel_ingestion_reports_freshness_and_recent_count(db_session):
    team = _team(db_session)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-101",
        when=now - timedelta(minutes=5),
        team=team,
    )
    _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-102",
        when=now - timedelta(minutes=30),
        team=team,
    )
    _receive(
        db_session,
        channel=InboxChannelType.email.value,
        wamid="mid-201",
        when=now - timedelta(minutes=60),
        team=team,
    )
    db_session.commit()

    observations = channel_health.collect_channel_ingestion_observations(
        db_session, now=now
    )
    by_key = {(o.signal, o.scope): o.value for o in observations}

    # Freshness is measured from the newest inbound row per channel.
    assert by_key[("seconds_since_last_inbound", "whatsapp")] == pytest.approx(300)
    assert by_key[("seconds_since_last_inbound", "email")] == pytest.approx(3600)
    # Only the 5-min-old whatsapp row falls inside the 15-minute window.
    assert by_key[("inbound_count_15m", "whatsapp")] == 1
    assert by_key[("inbound_count_15m", "email")] == 0


def test_duplicate_inbound_increments_suppression_counter(db_session):
    team = _team(db_session)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    labels = {"channel": InboxChannelType.whatsapp.value}
    before = _counter("sub_inbound_dedup_suppressed_total", labels)

    first = _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-dup",
        when=now,
        team=team,
    )
    db_session.commit()
    assert first.kind == "received"

    second = _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-dup",
        when=now,
        team=team,
    )
    db_session.commit()

    assert second.kind == "duplicate"
    after = _counter("sub_inbound_dedup_suppressed_total", labels)
    assert after == before + 1


def test_publish_survives_broker_failure_without_dropping_freshness(
    db_session, monkeypatch
):
    _team(db_session)
    published: list[tuple[str, str]] = []

    def _capture(domain, observations, *, status="ok", now=None):
        published.append((domain, status))
        return True

    def _broker_down():
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(channel_health, "publish_state_snapshot", _capture)
    monkeypatch.setattr(
        channel_health, "collect_celery_queue_observations", _broker_down
    )

    summary = channel_health.publish_channel_health(db_session)

    domains = {domain: status for domain, status in published}
    # Freshness still publishes; the queue snapshot degrades rather than raising.
    assert domains["channel_ingestion"] == "ok"
    assert domains["celery_queues"] == "degraded"
    assert summary["queue_status"] == "degraded"


def test_enabled_contract_without_history_reports_actionable_silence(
    db_session, monkeypatch
):
    default = channel_health_contracts.parse_channel_health_contracts(
        channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS
    )
    whatsapp = replace(
        next(item for item in default if item.channel == "whatsapp"),
        enabled=True,
        disabled_reason=None,
    )
    monkeypatch.setattr(
        channel_health_contracts,
        "load_channel_health_contracts",
        lambda _db: (whatsapp,),
    )
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)  # 09:00 WAT

    observations = channel_health.collect_channel_ingestion_observations(
        db_session,
        now=now,
    )
    by_key = {(item.signal, item.scope): item.value for item in observations}

    assert by_key[("contract_enabled", "whatsapp")] == 1
    assert by_key[("monitoring_active", "whatsapp")] == 1
    assert by_key[("history_present", "whatsapp")] == 0
    assert by_key[("silence_age_seconds", "whatsapp")] == 7_200
    assert by_key[("max_quiet_seconds", "whatsapp")] == 1_800


def test_verified_probe_does_not_mask_natural_traffic_silence(db_session, monkeypatch):
    team = _team(db_session)
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="natural-001",
        when=now - timedelta(minutes=20),
        team=team,
    )
    probe = _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="probe-001",
        when=now - timedelta(minutes=1),
        team=team,
    )
    probe_row = db_session.get(InboxMessage, probe.message_id)
    probe_row.metadata_ = {
        **dict(probe_row.metadata_ or {}),
        "smtp_probe_verified": True,
    }
    db_session.commit()

    default = channel_health_contracts.parse_channel_health_contracts(
        channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS
    )
    whatsapp = replace(
        next(item for item in default if item.channel == "whatsapp"),
        enabled=True,
        disabled_reason=None,
        monitoring_mode="hybrid",
        synthetic_max_age_seconds=1_800,
    )
    monkeypatch.setattr(
        channel_health_contracts,
        "load_channel_health_contracts",
        lambda _db: (whatsapp,),
    )

    observations = channel_health.collect_channel_ingestion_observations(
        db_session,
        now=now,
    )
    by_key = {(item.signal, item.scope): item.value for item in observations}

    assert by_key[("seconds_since_last_inbound", "whatsapp")] == 1_200
    assert by_key[("inbound_count_15m", "whatsapp")] == 0
    assert by_key[("silence_age_seconds", "whatsapp")] == 1_200
    assert by_key[("synthetic_age_seconds", "whatsapp")] == 60


def test_invalid_contract_registry_publishes_error_snapshot(db_session, monkeypatch):
    published: list[tuple[str, str, int]] = []

    def _invalid(_db):
        raise channel_health_contracts.ChannelHealthContractError("invalid")

    def _capture(domain, observations, *, status="ok", now=None):
        published.append((domain, status, len(list(observations))))
        return True

    monkeypatch.setattr(
        channel_health_contracts,
        "load_channel_health_contracts",
        _invalid,
    )
    monkeypatch.setattr(channel_health, "publish_state_snapshot", _capture)
    monkeypatch.setattr(channel_health, "collect_celery_queue_observations", lambda: [])

    summary = channel_health.publish_channel_health(db_session)

    assert ("channel_ingestion", "error", 0) in published
    assert summary["contract_status"] == "error"


def test_ingestion_snapshot_excludes_internal_channels(db_session, monkeypatch):
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    conversation = InboxConversation(channel_type=InboxChannelType.email.value)
    db_session.add(conversation)
    db_session.flush()
    for channel in InboxChannelType:
        db_session.add(
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=channel.value,
                direction="inbound",
                body="health cardinality test",
                received_at=now,
            )
        )
    db_session.commit()
    monkeypatch.setattr(
        channel_health_contracts,
        "load_channel_health_contracts",
        lambda _db: channel_health_contracts.parse_channel_health_contracts(
            channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS
        ),
    )

    observations = channel_health.collect_channel_ingestion_observations(
        db_session, now=now
    )
    scopes = {observation.scope for observation in observations}

    assert InboxChannelType.note.value not in scopes
    assert InboxChannelType.field_job.value not in scopes
    assert len({(item.signal, item.scope) for item in observations}) == 96


def test_snapshot_publish_failure_does_not_abort_other_domain(db_session, monkeypatch):
    published: list[str] = []

    def _capture(domain, observations, *, status="ok", now=None):
        published.append(domain)
        if domain == "channel_ingestion":
            raise ValueError("snapshot exceeds registered cardinality")
        return True

    monkeypatch.setattr(channel_health, "publish_state_snapshot", _capture)
    monkeypatch.setattr(
        channel_health,
        "collect_channel_ingestion_observations",
        lambda _db, now=None: [],
    )
    monkeypatch.setattr(channel_health, "collect_celery_queue_observations", lambda: [])

    summary = channel_health.publish_channel_health(db_session)

    assert published == ["channel_ingestion", "celery_queues"]
    assert summary["contract_status"] == "error"
    assert summary["queue_status"] == "ok"


def test_webhook_observation_records_each_outcome():
    labels = {"provider": "obs_test", "event": "unit"}

    def outcome(name: str) -> float:
        return _counter("sub_webhook_events_total", {**labels, "outcome": name})

    accepted_before = outcome("accepted")
    with webhook_observation(provider="obs_test", event="unit"):
        pass
    assert outcome("accepted") == accepted_before + 1

    rejected_before = outcome("rejected")
    with pytest.raises(HTTPException):
        with webhook_observation(provider="obs_test", event="unit"):
            raise HTTPException(status_code=401)
    assert outcome("rejected") == rejected_before + 1

    error_before = outcome("error")
    with pytest.raises(ValueError):
        with webhook_observation(provider="obs_test", event="unit"):
            raise ValueError("boom")
    assert outcome("error") == error_before + 1
