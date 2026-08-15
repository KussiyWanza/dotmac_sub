from __future__ import annotations

from argparse import ArgumentTypeError, Namespace
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from scripts.network import crm_network_map_point_migration as command
from scripts.network import stage_crm_network_map as staging_command


class _Outcome:
    def to_dict(self) -> dict[str, object]:
        return {"status": "ok"}


def test_read_command_uses_only_the_read_only_snapshot(monkeypatch):
    observed: list[str] = []

    @contextmanager
    def read_session():
        observed.append("read")
        yield object()

    @contextmanager
    def forbidden_write_session():
        observed.append("write")
        raise AssertionError("read command opened a write-capable session")
        yield  # pragma: no cover

    monkeypatch.setattr(command, "read_only_snapshot_session", read_session)
    monkeypatch.setattr(
        command.db_session_adapter,
        "owner_command_session",
        forbidden_write_session,
    )
    monkeypatch.setattr(
        command,
        "select_authoritative_crm_point_batches",
        lambda db, *, expected_archive_sha256: (),
    )

    result = command._run_read_command(
        Namespace(command="select", expected_archive_sha256=None)
    )

    assert result == {"selections": []}
    assert observed == ["read"]


@pytest.mark.parametrize("command_name", ["propose-batch", "apply-approved"])
def test_write_commands_use_only_the_owner_command_session(
    monkeypatch, command_name: str
):
    observed: list[str] = []
    transaction_free_session = object()

    @contextmanager
    def forbidden_read_session():
        observed.append("read")
        raise AssertionError("write command opened a read-only session")
        yield  # pragma: no cover

    @contextmanager
    def write_session():
        observed.append("write")
        yield transaction_free_session

    def propose(db, **kwargs):
        assert db is transaction_free_session
        assert kwargs["expected_archive_sha256"] == "a" * 64
        return _Outcome()

    def apply(db, **kwargs):
        assert db is transaction_free_session
        assert kwargs["expected_manifest_sha256"] == "b" * 64
        return _Outcome()

    monkeypatch.setattr(command, "read_only_snapshot_session", forbidden_read_session)
    monkeypatch.setattr(
        command.db_session_adapter, "owner_command_session", write_session
    )
    monkeypatch.setattr(command, "propose_crm_point_identity_proposals", propose)
    monkeypatch.setattr(command, "execute_crm_point_identity_apply", apply)
    args = Namespace(
        command=command_name,
        expected_archive_sha256="a" * 64,
        expected_manifest_sha256="b" * 64,
        actor="operator@example.com",
        reason="Reviewed migration",
        batch_id="batch-id",
        limit=50,
    )

    result = command._run_write_command(args)

    assert result == {"status": "ok"}
    assert observed == ["write"]


def test_snapshot_capture_time_is_typed_normalized_provenance():
    captured_at = staging_command._snapshot_captured_at("2026-08-15T09:30:00+01:00")

    assert captured_at == datetime(2026, 8, 15, 8, 30, tzinfo=UTC)
    assert captured_at.isoformat() == "2026-08-15T08:30:00+00:00"


@pytest.mark.parametrize(
    "value",
    ["not-a-timestamp", "2026-08-15T09:30:00"],
)
def test_snapshot_capture_time_fails_closed(value: str):
    with pytest.raises(ArgumentTypeError):
        staging_command._snapshot_captured_at(value)
