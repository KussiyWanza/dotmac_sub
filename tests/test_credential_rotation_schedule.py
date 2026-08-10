from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dotmac_kernel.secret_sources import clear_secret_source, install_secret_source

from app.services import credential_rotation_schedule as rotation


@pytest.fixture
def managed_key():
    """The active key is one OpenBao owns — said the way the code now reads it.

    This used to INSERT a `bao://secret/settings/auth#credential_encryption_key`
    row into `domain_settings`. That row was never the key: it was a reference
    `get_encryption_key` dereferenced on the decryption path, which starter
    ADR-0009 forbids and which `app/services/kernel_secret_source.py` replaced
    by holding the material from boot.

    So "managed by OpenBao" is now "held", and `_managed_key_source` asks the
    held set. The guard it enforces is unchanged and is the one that matters:
    rotation writes a new key into OpenBao, so it may only proceed when the key
    this process is actually using came from there — a key pinned to a literal
    in the environment still blocks, which
    `test_static_environment_key_blocks_scheduled_rotation` pins.
    """

    class _Held:
        def load(self) -> dict[str, str]:
            return {rotation._CURRENT_FIELD: "current-key"}

    install_secret_source(_Held())
    yield
    clear_secret_source()


def _patch_settings(monkeypatch, *, auto_apply: bool = True) -> None:
    values = {
        "credential_rotation_enabled": True,
        "credential_rotation_auto_apply": auto_apply,
        "credential_rotation_interval_days": 90,
        "credential_rotation_grace_days": 7,
    }
    monkeypatch.setattr(
        rotation,
        "resolve_value",
        lambda _db, _domain, key: values[key],
    )


def test_static_environment_key_blocks_scheduled_rotation(db_session, monkeypatch):
    _patch_settings(monkeypatch)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "literal-key")

    result = rotation.evaluate_scheduled_rotation(db_session)

    assert result == {
        "status": "blocked",
        "reason": "static_environment_key",
        "rotated": False,
    }


def test_first_managed_run_initializes_rotation_clock(
    db_session, monkeypatch, managed_key
):
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {rotation._CURRENT_FIELD: "current-key"},
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"current-key")
    writes: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_write_keyring",
        lambda payload: writes.append(payload) or True,
    )

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert result["status"] == "initialized"
    assert writes[0][rotation._ROTATED_AT_FIELD] == now.isoformat()


def test_due_rotation_stages_dual_key_before_reencrypting(
    db_session, monkeypatch, managed_key
):
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {
            rotation._CURRENT_FIELD: "current-key",
            rotation._ROTATED_AT_FIELD: (now - timedelta(days=91)).isoformat(),
        },
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"current-key")
    monkeypatch.setattr(rotation, "generate_encryption_key", lambda: "new-key")
    calls: list[str] = []
    staged: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_write_keyring",
        lambda payload: calls.append("stage") or staged.append(payload) or True,
    )

    class _Result:
        updated_records = 4
        updated_values = 6

    def _rotate(*_args, **kwargs):
        calls.append("rotate")
        assert kwargs["old_key"] == "current-key"
        assert kwargs["new_key"] == "new-key"
        return _Result()

    monkeypatch.setattr(rotation, "rotate_credential_encryption_material", _rotate)
    monkeypatch.setattr(rotation, "clear_cache", lambda: None)

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert calls == ["stage", "rotate"]
    assert result["status"] == "rotated"
    assert staged[0][rotation._PREVIOUS_FIELD] == "current-key"
    assert staged[0][rotation._CURRENT_FIELD] == "new-key"


def test_grace_period_converges_before_previous_key_retirement(
    db_session, monkeypatch, managed_key
):
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {
            rotation._CURRENT_FIELD: "new-key",
            rotation._PREVIOUS_FIELD: "old-key",
            rotation._RETIRE_AFTER_FIELD: (now - timedelta(days=1)).isoformat(),
        },
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"new-key")

    class _Result:
        updated_records = 1
        updated_values = 1

    monkeypatch.setattr(
        rotation,
        "rotate_credential_encryption_material",
        lambda *_args, **_kwargs: _Result(),
    )
    retired: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_retire_previous_key",
        lambda payload: retired.append(payload) or True,
    )
    monkeypatch.setattr(rotation, "clear_cache", lambda: None)

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert result["status"] == "previous_key_retired"
    assert retired
