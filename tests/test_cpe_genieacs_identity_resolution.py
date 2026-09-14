"""Fail-closed CPE-to-GenieACS identity resolution."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.network import _resolve


class _SequencedDb:
    def __init__(self, *rows):
        self._rows = iter(rows)

    def scalars(self, _statement):
        return next(self._rows)


class _Client:
    def __init__(self, device_ids: tuple[str, ...] = ()):
        self._device_ids = device_ids

    def list_devices(self, **_kwargs):
        return [{"_id": device_id} for device_id in self._device_ids]


def _cpe():
    return SimpleNamespace(id=uuid4(), serial_number="HWTC617C994D")


def test_multiple_active_fk_rows_are_ambiguous_before_any_acs_lookup() -> None:
    db = _SequencedDb([SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())])

    result = _resolve.resolve_genieacs_for_cpe_with_reason(db, _cpe())

    assert result.status is _resolve.CpeGenieAcsResolutionStatus.ambiguous
    assert result.resolved_pair is None
    assert result.candidate_count == 2


def test_multiple_serial_matched_rows_are_ambiguous() -> None:
    db = _SequencedDb(
        [],
        [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())],
    )

    result = _resolve.resolve_genieacs_for_cpe_with_reason(db, _cpe())

    assert result.status is _resolve.CpeGenieAcsResolutionStatus.ambiguous
    assert result.resolved_pair is None
    assert result.candidate_count == 2


def test_multiple_live_default_acs_documents_are_ambiguous(monkeypatch) -> None:
    client = _Client(("001-A-HWTC617C994D", "002-B-HWTC617C994D"))
    db = _SequencedDb([], [])
    monkeypatch.setattr(_resolve.settings_spec, "resolve_value", lambda *_: uuid4())
    monkeypatch.setattr(
        _resolve,
        "_resolve_server_by_id",
        lambda *_: SimpleNamespace(base_url="http://acs.test"),
    )
    monkeypatch.setattr(_resolve, "create_genieacs_client", lambda *_: client)

    result = _resolve.resolve_genieacs_for_cpe_with_reason(db, _cpe())

    assert result.status is _resolve.CpeGenieAcsResolutionStatus.ambiguous
    assert result.resolved_pair is None
    assert result.candidate_count == 2


def test_exact_fk_identity_returns_typed_resolved_verdict(monkeypatch) -> None:
    client = _Client()
    linked = SimpleNamespace(
        id=uuid4(),
        acs_server_id=uuid4(),
        genieacs_device_id="00259E-HG8546M-48575443617C994D",
    )
    db = _SequencedDb([linked])
    monkeypatch.setattr(
        _resolve,
        "_resolve_server_by_id",
        lambda *_: SimpleNamespace(base_url="http://acs.test"),
    )
    monkeypatch.setattr(_resolve, "create_genieacs_client", lambda *_: client)

    result = _resolve.resolve_genieacs_for_cpe_with_reason(db, _cpe())

    assert result.status is _resolve.CpeGenieAcsResolutionStatus.resolved
    assert result.provenance == "resolved_via_cpe_device_fk"
    assert result.resolved_pair == (
        client,
        "00259E-HG8546M-48575443617C994D",
    )


def test_missing_serial_is_typed_unresolved() -> None:
    cpe = SimpleNamespace(id=uuid4(), serial_number=None)

    result = _resolve.resolve_genieacs_for_cpe_with_reason(_SequencedDb(), cpe)

    assert result.status is _resolve.CpeGenieAcsResolutionStatus.unresolved
    assert result.resolved_pair is None
