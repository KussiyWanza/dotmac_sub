from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app.models.domain_setting_history import DomainSettingHistory
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.team_inbox import InboxChannelType
from app.services import channel_health_contracts
from app.services.domain_settings import network_monitoring_settings
from app.services.settings_spec import get_spec

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _contracts():
    return channel_health_contracts.parse_channel_health_contracts(
        deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    )


def _contract(channel: str):
    return next(item for item in _contracts() if item.channel == channel)


#: Channels with no provider behind them, so nothing to be healthy or
#: unhealthy about: an internal note, and a job chat delivered in-app over the
#: conversation socket.
_NON_EXTERNAL_CHANNELS = {InboxChannelType.note, InboxChannelType.field_job}


def test_default_registry_covers_every_external_inbox_channel_once():
    actual = {contract.channel for contract in _contracts()}
    expected = {
        channel.value
        for channel in InboxChannelType
        if channel not in _NON_EXTERNAL_CHANNELS
    }

    assert actual == expected
    assert all(
        contract.enabled or contract.disabled_reason for contract in _contracts()
    )
    spec = get_spec(SettingDomain.network_monitoring, "channel_health_contracts")
    assert spec is not None
    assert spec.env_var is None
    assert spec.default == channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS


def test_registry_rejects_missing_and_implicitly_disabled_channels():
    missing = deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    missing["channels"] = missing["channels"][:-1]
    with pytest.raises(
        channel_health_contracts.ChannelHealthContractError,
        match="Missing channel health contracts",
    ):
        channel_health_contracts.parse_channel_health_contracts(missing)


def test_stored_registry_predating_new_channels_is_backfilled_with_defaults():
    stored = deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    stored["channels"] = [
        item
        for item in stored["channels"]
        if item["channel"] not in {"facebook_comment", "instagram_comment"}
    ]

    contracts = channel_health_contracts.parse_channel_health_contracts(
        channel_health_contracts.backfill_missing_supported_channels(stored)
    )

    channels = {contract.channel for contract in contracts}
    assert channels == set(channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS)
    backfilled = next(c for c in contracts if c.channel == "facebook_comment")
    assert backfilled.enabled is False
    assert backfilled.disabled_reason
    # The stored registry itself must stay untouched.
    assert (
        len(stored["channels"])
        == len(channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS) - 2
    )


def test_reconciliation_persists_missing_defaults_without_overwriting_operator_policy(
    db_session,
):
    stored = deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    stored["channels"] = [
        item
        for item in stored["channels"]
        if item["channel"]
        not in {"website_fiber", "facebook_comment", "instagram_comment"}
    ]
    whatsapp = next(
        item for item in stored["channels"] if item["channel"] == "whatsapp"
    )
    whatsapp["max_quiet_seconds"] = 4321
    setting = network_monitoring_settings.ensure_by_key(
        db_session,
        key=channel_health_contracts.CHANNEL_HEALTH_CONTRACTS_SETTING,
        value_type=SettingValueType.json,
        value_json=stored,
    )

    result = channel_health_contracts.reconcile_persisted_channel_health_contracts(
        db_session
    )
    db_session.refresh(setting)

    assert result.rows_updated == 1
    assert set(result.channels_added) == {
        "website_fiber",
        "facebook_comment",
        "instagram_comment",
    }
    persisted = channel_health_contracts.parse_channel_health_contracts(
        setting.value_json
    )
    persisted_whatsapp = next(item for item in persisted if item.channel == "whatsapp")
    assert persisted_whatsapp.max_quiet_seconds == 4321
    for channel in result.channels_added:
        contract = next(item for item in persisted if item.channel == channel)
        assert contract.enabled is False
        assert contract.disabled_reason


def test_reconciliation_is_idempotent(db_session):
    network_monitoring_settings.ensure_by_key(
        db_session,
        key=channel_health_contracts.CHANNEL_HEALTH_CONTRACTS_SETTING,
        value_type=SettingValueType.json,
        value_json=deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS),
    )
    before = db_session.query(DomainSettingHistory).count()

    result = channel_health_contracts.reconcile_persisted_channel_health_contracts(
        db_session
    )

    assert result.rows_updated == 0
    assert result.channels_added == ()
    assert db_session.query(DomainSettingHistory).count() == before


def test_runtime_backfill_warns_once_per_missing_channel_set(caplog):
    stored = deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    stored["channels"] = [
        item for item in stored["channels"] if item["channel"] != "website_fiber"
    ]
    channel_health_contracts._warn_backfilled_defaults_once.cache_clear()

    channel_health_contracts.backfill_missing_supported_channels(stored)
    channel_health_contracts.backfill_missing_supported_channels(stored)

    warnings = [
        record
        for record in caplog.records
        if "channel_health_contracts_backfilled_defaults" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_backfill_leaves_malformed_registries_for_the_parser_to_reject():
    assert channel_health_contracts.backfill_missing_supported_channels(None) is None
    malformed = {"version": 2, "channels": "nope"}
    assert (
        channel_health_contracts.backfill_missing_supported_channels(malformed)
        is malformed
    )
    with pytest.raises(channel_health_contracts.ChannelHealthContractError):
        channel_health_contracts.parse_channel_health_contracts(
            channel_health_contracts.backfill_missing_supported_channels(malformed)
        )

    implicit = deepcopy(channel_health_contracts.DEFAULT_CHANNEL_HEALTH_CONTRACTS)
    implicit["channels"][0].pop("disabled_reason")
    with pytest.raises(
        channel_health_contracts.ChannelHealthContractError,
        match="requires a reason",
    ):
        channel_health_contracts.parse_channel_health_contracts(implicit)


def test_active_window_uses_lagos_time_and_does_not_charge_closed_hours():
    whatsapp = replace(
        _contract("whatsapp"),
        enabled=True,
        disabled_reason=None,
    )
    open_time = datetime(2026, 7, 20, 7, 10, tzinfo=UTC)  # 08:10 WAT
    active, elapsed = channel_health_contracts.active_window_elapsed_seconds(
        whatsapp,
        now=open_time,
    )

    assert active is True
    assert elapsed == 4_200
    assert (
        channel_health_contracts.effective_age_seconds(
            whatsapp,
            observed_at=open_time - timedelta(days=1),
            now=open_time,
        )
        == 4_200
    )

    closed_time = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)  # 00:00 WAT
    assert channel_health_contracts.active_window_elapsed_seconds(
        whatsapp,
        now=closed_time,
    ) == (False, 0.0)


def test_continuous_contract_never_resets_silence_at_midnight():
    email = replace(_contract("email"), enabled=True, disabled_reason=None)
    now = datetime(2026, 7, 20, 23, 30, tzinfo=UTC)

    assert channel_health_contracts.active_window_elapsed_seconds(
        email,
        now=now,
    ) == (True, None)
    assert (
        channel_health_contracts.effective_age_seconds(
            email,
            observed_at=now - timedelta(hours=5),
            now=now,
        )
        == 18_000
    )


def test_alert_rules_enforce_contract_signals_at_both_severities():
    rules_path = PROJECT_ROOT / "deploy/observability/channel_observability.rules.yml"
    payload = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules = payload["groups"][0]["rules"]
    by_name = {rule["alert"]: rule for rule in rules}

    expected = {
        "ChannelNaturalIngestionSilentCritical",
        "ChannelNaturalIngestionSilentWarning",
        "ChannelSyntheticProbeStaleCritical",
        "ChannelSyntheticProbeStaleWarning",
        "ChannelHealthContractInvalid",
        "ChannelObserverMissing",
    }
    assert expected.issubset(by_name)
    for name in expected - {"ChannelHealthContractInvalid", "ChannelObserverMissing"}:
        expression = by_name[name]["expr"]
        assert "monitoring_active" in expression
        assert "severity_critical" in expression
        assert by_name[name]["labels"]["owner"] == "communications-team-inbox"
        assert by_name[name]["annotations"]["runbook"].endswith(
            "CHANNEL_OBSERVABILITY.md"
        )
    assert 'seconds_since_last_inbound"} > 3600' not in rules_path.read_text(
        encoding="utf-8"
    )
