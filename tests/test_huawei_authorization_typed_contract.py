"""ONT authorization carries a typed device verdict end to end.

The authorization workflow used to re-classify the adapter's operator-facing
message to decide whether the OLT had rejected the write because the serial was
already registered. By the time it looked, the message had been wrapped
("OLT rejected command: ...") and truncated, so the reuse/move recovery branch
was unreachable for every real firmware wording while a synthetic-message unit
test kept passing. These tests lock the typed path instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiDeviceOutcome,
)
from app.services.network.olt_protocol_adapters import (
    OltOperationResult,
    OltProtocolAdapter,
)
from app.services.network.olt_ssh_ont._common import OntAuthorizationOutcome
from app.services.network.ont_authorization import _is_serial_already_registered


def _olt() -> SimpleNamespace:
    return SimpleNamespace(name="Test OLT", model="MA5608T", vendor="huawei")


def _registration(fsp: str, onu_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        fsp=fsp, onu_id=onu_id, real_serial="48575443A31A3529", run_state="online"
    )


def _patch_authorize(monkeypatch, outcome: OntAuthorizationOutcome) -> None:
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.authorize_ont",
        lambda *args, **kwargs: outcome,
    )


# ---------------------------------------------------------------------------
# Adapter: rejection carries the code, acceptance is verified
# ---------------------------------------------------------------------------


def test_duplicate_serial_rejection_reaches_the_workflow_as_a_typed_code(
    monkeypatch,
) -> None:
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(
            HuaweiDeviceOutcome.rejected_by_device(
                "  Failure: The ONT SN already exists", action="ont add"
            )
        ),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is False
    assert result.response_code == HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS
    assert _is_serial_already_registered(result) is True


def test_accepted_with_an_ont_id_needs_no_readback(monkeypatch) -> None:
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(
            HuaweiDeviceOutcome.accepted("ONT authorized on port 0/1/2 (ONT-ID 3)"),
            ont_id=3,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda *args, **kwargs: pytest.fail("readback must not run"),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is True
    assert result.ont_id == 3


def test_silent_shelf_is_confirmed_by_readback_not_assumed_successful(
    monkeypatch,
) -> None:
    """The old code returned "command sent" as a success with no ONT-ID."""
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(
            HuaweiDeviceOutcome(
                succeeded=False,
                code=HuaweiCliErrorCode.NONE,
                message="OLT returned no verdict",
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda olt, serial: (True, "found", _registration("0/1/2", 9)),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is True
    assert result.ont_id == 9
    assert result.data["verified_registered"] is True


def test_silent_shelf_fails_when_the_serial_is_not_registered(monkeypatch) -> None:
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(
            HuaweiDeviceOutcome(
                succeeded=False,
                code=HuaweiCliErrorCode.NONE,
                message="OLT returned no verdict",
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda olt, serial: (True, "not registered", None),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is False
    assert result.data["verified_registered"] is False


def test_readback_on_a_different_port_is_not_a_successful_authorization(
    monkeypatch,
) -> None:
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(
            HuaweiDeviceOutcome.accepted("Number of ONTs that can be added: 1"),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda olt, serial: (True, "found", _registration("0/1/7", 4)),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is False
    assert "0/1/7" in result.message


def test_prefixed_target_port_is_canonicalized_before_readback_comparison(
    monkeypatch,
) -> None:
    """``gpon-0/1/2`` and ``0/1/2`` are the same port, and must compare equal."""
    _patch_authorize(
        monkeypatch,
        OntAuthorizationOutcome(HuaweiDeviceOutcome.accepted("accepted")),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda olt, serial: (True, "found", _registration("0/1/2", 9)),
    )

    result = OltProtocolAdapter(_olt()).authorize_ont(
        "gpon-0/1/2", "48575443A31A3529", line_profile_id=40, service_profile_id=41
    )

    assert result.success is True
    assert result.ont_id == 9


# ---------------------------------------------------------------------------
# The result contract itself
# ---------------------------------------------------------------------------


def test_typed_verdict_wins_over_the_operator_facing_message() -> None:
    """A wrapped message must never override the classified device code."""
    result = OltOperationResult(
        success=False,
        message="OLT rejected command: Failure: something a regex will not know",
        response_code=HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS,
    )

    assert result.response_code == HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS
    assert result.error_code == "serial_already_exists"
    assert result.data["huawei_cli_response"]["response_code"] == (
        "serial_already_exists"
    )


def test_untyped_results_still_recover_a_code_from_their_message() -> None:
    """Adapters not yet migrated keep working (NETCONF path, test doubles)."""
    result = OltOperationResult(
        success=False, message="OLT rejected: Failure: The ONT does not exist"
    )

    assert result.response_code == HuaweiCliErrorCode.ONT_NOT_EXIST


def test_workflow_predicate_falls_back_to_message_for_untyped_adapters() -> None:
    assert (
        _is_serial_already_registered(
            SimpleNamespace(message="Failure: SN already exists")
        )
        is True
    )
    assert (
        _is_serial_already_registered(
            SimpleNamespace(message="Failure: ONT is offline")
        )
        is False
    )


# ---------------------------------------------------------------------------
# F/S/P canonicalization at the command boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefixed", ["gpon-0/1/0", "GPON-0/1/0", "pon-0/1/0", "xgpon-0/1/0"]
)
def test_service_port_command_never_carries_a_port_name_prefix(prefixed: str) -> None:
    """``PonPort.name`` values reach command builders; prefixes must not survive.

    The boolean validator normalizes prefixes before matching but returns only
    a verdict, so ``gpon-0/1/0`` used to be accepted and emitted verbatim as
    ``service-port ... gpon gpon-0/1/0 ...``.
    """
    from app.services.network.olt_command_gen import build_service_port_command

    command = build_service_port_command(
        fsp=prefixed, ont_id=3, gem_index=1, vlan_id=203
    )

    assert "gpon 0/1/0 " in command
    assert "gpon-" not in command


def test_service_port_command_rejects_an_unusable_fsp() -> None:
    from app.services.network.olt_command_gen import build_service_port_command

    with pytest.raises(ValueError, match="Invalid F/S/P"):
        build_service_port_command(fsp="uplink-A", ont_id=3, gem_index=1, vlan_id=203)


def test_vendor_adapter_commands_are_built_from_canonical_parts() -> None:
    from app.services.network.olt_vendor_adapters import HuaweiOltAdapter

    commands = HuaweiOltAdapter().generate_delete_ont_commands("gpon-0/2/13", 7)

    assert commands[0] == "interface gpon 0/2"
    assert commands[1] == "ont delete 13 7"


def test_strict_validator_returns_canonical_parts_and_keeps_range_checks() -> None:
    from app.services.network.olt_validators import (
        ValidationError,
        validate_fsp,
        validate_fsp_parts,
    )

    assert validate_fsp("gpon-0/2/1") == "0/2/1"
    parts = validate_fsp_parts("xgpon-0/2/1")
    assert (parts.frame, parts.slot, parts.port) == ("0", "2", "1")
    assert parts.frame_slot == "0/2"

    with pytest.raises(ValidationError):
        validate_fsp("0/99/1")
