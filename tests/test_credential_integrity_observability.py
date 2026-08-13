from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from app.models.catalog import AccessCredential, NasDevice
from app.models.subscriber import Subscriber
from app.services import credential_key_rotation as key_rotation
from app.services import credential_rotation_schedule as rotation_schedule
from app.services.credential_crypto import (
    decrypt_credential_with_key,
    encrypt_credential_with_key,
)
from app.services.credential_key_rotation import CredentialIntegrityResult
from app.services.observability import StateObservation


def _integrity_result(
    *,
    plaintext: int = 0,
    undecryptable: int = 0,
) -> CredentialIntegrityResult:
    counts = {
        "NasDevice.shared_secret": {
            "encrypted": 1,
            "plaintext": plaintext,
            "one_way": 0,
            "undecryptable": undecryptable,
            "reference": 0,
            "empty": 0,
        }
    }
    return CredentialIntegrityResult(
        counts=counts,
        totals={
            "encrypted": 1,
            "plaintext": plaintext,
            "one_way": 0,
            "undecryptable": undecryptable,
            "reference": 0,
            "empty": 0,
        },
        scanned_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_integrity_scan_classifies_without_exposing_values(db_session, monkeypatch):
    key = Fernet.generate_key()
    row = NasDevice(
        name="integrity-test",
        shared_secret=encrypt_credential_with_key("radius-secret", key),
        ssh_password="plain:ssh-secret",
        api_password="enc:not-valid",
        snmp_community="bao://secret/network#community",
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)

    result = key_rotation.scan_credential_encryption_integrity(db_session)

    assert result.counts["NasDevice.shared_secret"]["encrypted"] == 1
    assert result.counts["NasDevice.ssh_password"]["plaintext"] == 1
    assert result.counts["NasDevice.api_password"]["undecryptable"] == 1
    assert result.counts["NasDevice.snmp_community"]["reference"] == 1
    serialized = json.dumps(
        {"counts": result.counts, "observations": result.observations()}
    )
    assert str(row.id) not in serialized
    assert "radius-secret" not in serialized
    assert "ssh-secret" not in serialized


def test_integrity_scan_reads_ont_credentials_through_network_owner(
    db_session, monkeypatch
):
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: None)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        key_rotation,
        "desired_config_values_for_paths",
        lambda _db, paths: [(paths[0], "plain:wifi-secret")],
    )

    result = key_rotation.scan_credential_encryption_integrity(db_session)

    scope = "OntUnit.desired_config.wifi.password"
    assert result.counts[scope]["plaintext"] == 1


def test_integrity_scan_selects_only_owned_credential_columns(db_session, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        key_rotation.scan_credential_encryption_integrity(db_session)
    finally:
        event.remove(bind, "before_cursor_execute", capture)

    access_query = next(
        statement for statement in statements if "FROM access_credentials" in statement
    )
    assert "secret_hash" in access_query
    assert "subscription_id" not in access_query


def test_rotation_loads_only_owned_access_credential_columns(db_session, subscriber):
    key = Fernet.generate_key()
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="schema-tolerant-rotation",
        secret_hash="plain:secret",
    )
    db_session.add(credential)
    db_session.commit()
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        key_rotation._rotate_model_fields(
            db_session,
            AccessCredential,
            ("secret_hash",),
            old_key=key.decode("ascii"),
            new_key=key.decode("ascii"),
        )
    finally:
        event.remove(bind, "before_cursor_execute", capture)

    access_query = next(
        statement for statement in statements if "FROM access_credentials" in statement
    )
    assert "secret_hash" in access_query
    assert "subscription_id" not in access_query


def test_credential_remediation_dry_run_does_not_mutate(db_session, monkeypatch):
    key = Fernet.generate_key()
    row = NasDevice(name="remediation-dry-run", shared_secret="legacy-secret")
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    published: list[str] = []
    monkeypatch.setattr(
        key_rotation,
        "publish_credential_integrity_snapshot",
        lambda _integrity, *, operation, operation_status, **_kwargs: (
            published.append(f"{operation}:{operation_status}") or True
        ),
    )

    result = key_rotation.remediate_credential_encryption(db_session)

    db_session.refresh(row)
    assert result.status == "dry_run"
    assert result.plaintext_before >= 1
    assert result.updated_values == 0
    assert row.shared_secret == "legacy-secret"
    assert published == ["remediation:dry_run"]


def test_credential_remediation_converges_and_is_idempotent(db_session, monkeypatch):
    key = Fernet.generate_key()
    row = NasDevice(name="remediation-execute", shared_secret="legacy-secret")
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        key_rotation,
        "publish_credential_integrity_snapshot",
        lambda *_args, **_kwargs: True,
    )

    first = key_rotation.remediate_credential_encryption(db_session, execute=True)
    db_session.refresh(row)

    assert first.status == "completed"
    assert first.updated_values == 1
    assert first.plaintext_after == 0
    assert decrypt_credential_with_key(row.shared_secret, key) == "legacy-secret"

    second = key_rotation.remediate_credential_encryption(db_session, execute=True)

    assert second.status == "completed"
    assert second.updated_values == 0


def test_credential_remediation_blocks_before_writes_on_corruption(
    db_session, monkeypatch
):
    key = Fernet.generate_key()
    row = NasDevice(
        name="remediation-corrupt",
        shared_secret="legacy-secret",
        api_password="enc:not-valid",
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        key_rotation,
        "publish_credential_integrity_snapshot",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        key_rotation,
        "rotate_credential_encryption_material",
        lambda *_args, **_kwargs: pytest.fail("blocked remediation must not write"),
    )

    result = key_rotation.remediate_credential_encryption(db_session, execute=True)

    db_session.refresh(row)
    assert result.status == "blocked"
    assert result.reason == "undecryptable_credentials"
    assert result.undecryptable >= 1
    assert row.shared_secret == "legacy-secret"


def test_credential_remediation_preserves_one_way_access_hash(db_session, monkeypatch):
    key = Fernet.generate_key()
    subscriber = Subscriber(
        first_name="Opaque",
        last_name="Credential",
        email="opaque-credential@example.com",
        user_type="customer",
    )
    db_session.add(subscriber)
    db_session.flush()
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="opaque-credential",
        secret_hash="YWJjZGVmZ2hpamtsbW5vcA==",
    )
    db_session.add(credential)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        key_rotation,
        "publish_credential_integrity_snapshot",
        lambda *_args, **_kwargs: True,
    )

    before = key_rotation.scan_credential_encryption_integrity(db_session)
    result = key_rotation.remediate_credential_encryption(db_session, execute=True)
    db_session.refresh(credential)

    scope = "AccessCredential.secret_hash"
    assert before.counts[scope]["one_way"] == 1
    assert before.counts[scope]["plaintext"] == 0
    assert result.status == "completed"
    assert result.updated_values == 0
    assert credential.secret_hash == "YWJjZGVmZ2hpamtsbW5vcA=="


def test_shared_observability_snapshot_round_trip(monkeypatch):
    from app.services import app_cache, observability

    stored: dict[str, object] = {}

    def fake_set(key, value, ttl):
        stored.update({"key": key, "value": value, "ttl": ttl})
        return True

    monkeypatch.setattr(app_cache, "set_json", fake_set)
    monkeypatch.setattr(app_cache, "get_json", lambda key: stored.get("value"))

    assert observability.publish_state_snapshot(
        "credentials",
        [
            StateObservation(
                signal="undecryptable",
                scope="NasDevice.shared_secret",
                value=2,
            )
        ],
        status="error",
        now=datetime(2026, 7, 12, tzinfo=UTC),
    )
    loaded = observability.load_state_snapshot("credentials")

    assert loaded is not None
    assert loaded["status"] == "error"
    assert loaded["observations"] == [
        {
            "signal": "undecryptable",
            "scope": "NasDevice.shared_secret",
            "value": 2.0,
        }
    ]
    assert int(stored["ttl"]) == 7 * 86_400


def test_credential_snapshot_preserves_rotation_metric_contract(monkeypatch):
    from app.services import observability

    captured: dict[str, object] = {}

    def fake_publish(domain, observations, *, status, now):
        captured.update(
            {
                "domain": domain,
                "observations": list(observations),
                "status": status,
                "now": now,
            }
        )
        return True

    monkeypatch.setattr(observability, "publish_state_snapshot", fake_publish)
    integrity = _integrity_result(plaintext=2)

    assert key_rotation.publish_credential_integrity_snapshot(
        integrity,
        operation="rotation",
        operation_status="not_due",
        extra_observations=(("managed_key_source", "openbao_env_ref", 1.0),),
    )

    assert captured["domain"] == "credentials"
    assert captured["status"] == "degraded"
    observations = captured["observations"]
    assert isinstance(observations, list)
    labels = {
        (item.signal, item.scope): item.value
        for item in observations
        if isinstance(item, StateObservation)
    }
    assert labels[("rotation_status", "not_due")] == 1.0
    assert labels[("managed_key_source", "openbao_env_ref")] == 1.0


def test_shared_observability_rejects_unbounded_scope():
    from app.services.observability import publish_state_snapshot

    with pytest.raises(ValueError, match="Invalid observability scope"):
        publish_state_snapshot(
            "credentials",
            [StateObservation(signal="plaintext", scope="record id=123", value=1)],
        )


def test_generic_collector_exports_credential_snapshot(monkeypatch):
    from app import metrics
    from app.services import observability

    snapshot = {
        "domain": "credentials",
        "status": "degraded",
        "observed_at": datetime.now(UTC).isoformat(),
        "observations": [
            {
                "signal": "plaintext",
                "scope": "all",
                "value": 3,
            }
        ],
    }
    monkeypatch.setattr(
        observability,
        "load_state_snapshot",
        lambda domain: snapshot if domain == "credentials" else None,
    )

    families = list(metrics._ObservabilityStateCollector().collect())
    by_name = {family.name: family for family in families}
    samples = by_name["observability_state"].samples

    assert any(
        sample.labels
        == {"domain": "credentials", "signal": "plaintext", "scope": "all"}
        and sample.value == 3
        for sample in samples
    )
    status_samples = by_name["observability_snapshot_status"].samples
    assert any(
        sample.labels == {"domain": "credentials", "status": "degraded"}
        and sample.value == 1
        for sample in status_samples
    )


def test_daily_rotation_blocks_on_undecryptable_inventory(db_session, monkeypatch):
    integrity = _integrity_result(undecryptable=2)

    @contextmanager
    def fake_lock(*_args, **_kwargs):
        yield db_session, True

    monkeypatch.setattr(
        rotation_schedule.db_session_adapter,
        "advisory_lock",
        fake_lock,
    )
    monkeypatch.setattr(
        rotation_schedule,
        "scan_credential_encryption_integrity",
        lambda _db: integrity,
    )
    monkeypatch.setattr(
        rotation_schedule,
        "_managed_key_source",
        lambda: (True, "openbao_env_ref"),
    )
    monkeypatch.setattr(
        rotation_schedule,
        "evaluate_scheduled_rotation",
        lambda _db: pytest.fail("rotation must not run with corrupt ciphertext"),
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        rotation_schedule,
        "_publish_integrity_state",
        lambda *_args, **kwargs: published.append(kwargs),
    )

    result = rotation_schedule.run_scheduled_credential_rotation()

    assert result["status"] == "blocked"
    assert result["reason"] == "credential_integrity_failed"
    assert result["integrity_undecryptable"] == 2
    assert published[0]["key_source"] == "openbao_env_ref"


def test_security_task_records_lock_contention_as_skip(monkeypatch):
    from app.tasks import security

    monkeypatch.setattr(
        security,
        "run_rotation",
        lambda: {"status": "already_running", "rotated": False},
    )
    skips: list[tuple[str, str]] = []
    monkeypatch.setattr(
        security,
        "record_task_skip",
        lambda task, *, reason: skips.append((task, reason)) or 1,
    )
    monkeypatch.setattr(
        security,
        "record_task_run",
        lambda *_args, **_kwargs: pytest.fail("skip must not record success"),
    )

    result = security.run_scheduled_credential_rotation.run()

    assert result["status"] == "already_running"
    assert skips == [(security._TASK_NAME, "already_running")]


def test_remediation_cli_emits_aggregate_json(monkeypatch, capsys):
    from scripts.one_off import remediate_credential_encryption as cli

    class _DummySession:
        def rollback(self):
            return None

        def close(self):
            return None

    result = key_rotation.CredentialRemediationResult(
        status="dry_run",
        execute=False,
        values_scanned=12,
        plaintext_before=3,
        plaintext_after=3,
        undecryptable=0,
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(
        cli,
        "remediate_credential_encryption",
        lambda _db, *, execute: result,
    )

    assert cli.main(["--dry-run"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == result.as_dict()
    assert "id" not in payload


def test_remediation_cli_redacts_service_exception_details(monkeypatch, capsys):
    from scripts.one_off import remediate_credential_encryption as cli

    class _DummySession:
        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(
        cli,
        "remediate_credential_encryption",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Failed to rotate NasDevice.shared_secret id=customer-123")
        ),
    )

    assert cli.main(["--execute"]) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "Credential remediation failed."
    assert "customer-123" not in json.dumps(payload)
