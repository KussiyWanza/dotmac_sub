from __future__ import annotations

import pytest

from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiCliResource,
    classify_huawei_cli_response,
    describe_huawei_rejection,
    has_huawei_cli_error,
    is_huawei_cli_unsupported,
    is_huawei_no_autofind_entries,
    is_huawei_resource_absent,
    is_huawei_serial_already_registered,
    parse_huawei_ont_add_result,
    project_huawei_result_evidence,
    project_response_code_evidence,
)
from app.services.network.olt_ssh_session import ErrorCode, parse_command_result
from app.services.network.parsers.cli import is_error_output


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("Failure: The VLAN does not exist", HuaweiCliErrorCode.VLAN_NOT_EXIST),
        ("Failure: The ONT does not exist", HuaweiCliErrorCode.ONT_NOT_EXIST),
        ("The required ONT does not exist", HuaweiCliErrorCode.ONT_NOT_EXIST),
        ("Failure: ONT is not online", HuaweiCliErrorCode.ONT_OFFLINE),
        (
            "Failure: The service virtual port does not exist",
            HuaweiCliErrorCode.SERVICE_PORT_NOT_EXIST,
        ),
        (
            "OLT rejected: Failure: The profile does not exist",
            HuaweiCliErrorCode.PROFILE_NOT_EXIST,
        ),
        ("% Parameter error", HuaweiCliErrorCode.PARAMETER_ERROR),
        ("% Unknown command", HuaweiCliErrorCode.UNKNOWN_COMMAND),
        ("Failure: insufficient privilege", HuaweiCliErrorCode.PERMISSION_DENIED),
        ("Failure: resource is busy", HuaweiCliErrorCode.RESOURCE_BUSY),
        ("Failure: new firmware wording", HuaweiCliErrorCode.UNKNOWN_ERROR),
    ],
)
def test_classifies_known_huawei_responses(
    output: str,
    code: HuaweiCliErrorCode,
) -> None:
    response = classify_huawei_cli_response(output)

    assert response.error_code == code
    assert response.has_error_marker is True
    assert response.accepted is False


def test_success_output_does_not_match_customer_text() -> None:
    output = """
    F/S/P  ONT-ID  Description
    0/2/1  1       Error Systems Ltd
    0/2/1  2       Invalid Address Holdings
    0/2/1  3       Locked Gates Limited
    0/2/1  4       offline  Unknown Command Consulting
    """

    response = classify_huawei_cli_response(output)

    assert response.error_code == HuaweiCliErrorCode.NONE
    assert response.accepted is True
    assert has_huawei_cli_error(output) is False


def test_autofind_empty_marker_is_a_known_empty_success() -> None:
    output = "Failure: Automatically found ONTs do not exist"

    response = classify_huawei_cli_response(output)

    assert response.error_code == HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES
    assert response.accepted is True
    assert response.has_error_marker is False
    assert is_huawei_no_autofind_entries(output) is True


def test_serial_conflict_is_not_generic_idempotent_success() -> None:
    output = "Failure: SN already exists"

    response = classify_huawei_cli_response(output)

    assert response.error_code == HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS
    assert response.accepted is False
    assert is_huawei_serial_already_registered(output) is True


def test_operation_evidence_is_stable_and_excludes_raw_cli() -> None:
    response = classify_huawei_cli_response("% Unknown command secret-value")

    evidence = response.to_evidence()

    assert evidence == {
        "classifier": "huawei_cli_response",
        "schema_version": 1,
        "response_code": "unknown_command",
        "accepted": False,
        "has_error_marker": True,
        "idempotent_success": False,
        "resource_absent": False,
        "unsupported": True,
        "retryable": False,
    }
    assert "secret-value" not in repr(evidence)


def test_adapter_result_evidence_projection_preserves_transport_code() -> None:
    result = type(
        "Result",
        (),
        {
            "error_code": "TimeoutError",
            "data": {
                "huawei_cli_response": {
                    "response_code": "unknown_command",
                    "unsupported": True,
                }
            },
        },
    )()

    assert project_huawei_result_evidence(result) == {
        "error_code": "TimeoutError",
        "huawei_cli_response": {
            "response_code": "unknown_command",
            "unsupported": True,
        },
    }


def test_generic_already_exists_preserves_session_idempotency() -> None:
    result = parse_command_result("Failure: profile already exists")

    assert result.success is True
    assert result.error_code == ErrorCode.ALREADY_EXISTS
    assert result.is_idempotent_success is True


def test_absence_is_resource_specific() -> None:
    ont_missing = "OLT rejected: Failure: The ONT does not exist"

    assert is_huawei_resource_absent(ont_missing, HuaweiCliResource.ONT) is True
    assert (
        is_huawei_resource_absent(ont_missing, HuaweiCliResource.SERVICE_PORT) is False
    )


def test_service_port_wrappers_and_readback_wording_are_classified() -> None:
    assert is_huawei_resource_absent(
        "Service-port 217 was not found",
        HuaweiCliResource.SERVICE_PORT,
    )
    assert is_huawei_resource_absent(
        "OLT rejected: Failure: The service port does not exist",
        HuaweiCliResource.SERVICE_PORT,
    )


def test_parameter_error_is_only_unsupported_when_caller_allows_fallback() -> None:
    output = "% Parameter error"

    assert is_huawei_cli_unsupported(output) is False
    assert (
        is_huawei_cli_unsupported(output, parameter_error_is_unsupported=True) is True
    )


def test_legacy_error_predicates_delegate_to_canonical_classifier() -> None:
    rejected = "OLT rejected: Failure: unsupported firmware response"
    customer_text = "Description: Invalid Address Holdings"

    assert is_error_output(rejected) is True
    assert is_error_output(customer_text) is False


# ---------------------------------------------------------------------------
# Verbatim device fixtures.
#
# These strings are copied from real shelves, not paraphrased. A paraphrased
# fixture ("Automatically found ONTs do not exist", without the leading "The")
# is exactly what kept CI green for weeks while BOI and Gudu autofind reads
# failed in production.
# ---------------------------------------------------------------------------

#: BOI (MA5608T) and Gudu, direct SSH reads 2026-07-20 / 07-23 / 07-24.
BOI_GUDU_EMPTY_AUTOFIND = "  Failure: The automatically found ONTs do not exist"


def test_verbatim_empty_autofind_from_boi_and_gudu_is_a_known_empty() -> None:
    response = classify_huawei_cli_response(BOI_GUDU_EMPTY_AUTOFIND)

    assert response.error_code == HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES
    assert response.accepted is True
    assert response.has_error_marker is False
    assert is_huawei_no_autofind_entries(BOI_GUDU_EMPTY_AUTOFIND) is True


@pytest.mark.parametrize(
    "output",
    [
        "Failure: SN already exists",
        "  Failure: The ONT SN already exists",
        "  Failure: The SN already exists in the port",
        "  Failure: SN 48575443A31A3529 already exists",
        "  Failure: The SN has been used by another ONT",
        "  Failure: The ONT with the same SN already exists",
        "  Failure: The SN is conflicted with the SN of an existing ONT",
    ],
)
def test_duplicate_serial_wordings_all_reach_the_reuse_branch(output: str) -> None:
    """Every firmware wording must reach ont_authorization's reuse/move path.

    Anything that classifies as generic ALREADY_EXISTS or UNKNOWN_ERROR instead
    makes re-authorizing a registered ONT a hard operator-facing failure.
    """
    assert is_huawei_serial_already_registered(output) is True


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (
            "Failure: The service virtual port has existed already",
            HuaweiCliErrorCode.ALREADY_EXISTS,
        ),
        ("  Failure: The profile already exists", HuaweiCliErrorCode.ALREADY_EXISTS),
        ("Failure: The ONT does not exist", HuaweiCliErrorCode.ONT_NOT_EXIST),
    ],
)
def test_non_serial_conflicts_keep_their_own_codes(
    output: str, code: HuaweiCliErrorCode
) -> None:
    """The broadened serial patterns must not swallow other conflicts."""
    assert classify_huawei_cli_response(output).error_code == code


@pytest.mark.parametrize(
    "wrapper",
    [
        "OLT rejected: {}",
        "OLT rejected command: {}",
        "OLT rejected upgrade: {}",
        "OLT rejected inventory read for 0/1/2: {}",
        "OLT error: {}",
    ],
)
def test_every_operator_wrapper_still_classifies(wrapper: str) -> None:
    """Wrapping for operators must not erase the device verdict.

    ``authorize_ont`` wrapped rejections as "OLT rejected command: ..." while
    the envelope only accepted the bare "OLT rejected:" form, so a genuine
    duplicate-serial rejection classified as NONE — no error at all.
    """
    wrapped = wrapper.format("Failure: SN already exists")

    assert classify_huawei_cli_response(wrapped).error_code == (
        HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS
    )


def test_canonical_rejection_wrapper_round_trips_through_the_classifier() -> None:
    message = describe_huawei_rejection(
        "ont add 0 sn-auth 4857 omci\r\n  Failure: The ONT does not exist\r\nOLT#",
        action="ont add",
    )

    assert message.startswith("OLT rejected: ")
    assert "(ont add)" in message
    assert classify_huawei_cli_response(message).error_code == (
        HuaweiCliErrorCode.ONT_NOT_EXIST
    )


# ---------------------------------------------------------------------------
# ``ont add`` acceptance
# ---------------------------------------------------------------------------

_ADD_ECHO = (
    "ont add 0 sn-auth 48575443A31A3529 omci ont-lineprofile-id 40 "
    'ont-srvprofile-id 41 desc "x"'
)


def test_ont_add_accepted_on_success_counter_without_an_ont_id() -> None:
    result = parse_huawei_ont_add_result(
        f"{_ADD_ECHO}\r\n  Number of ONTs that can be added: 1, success: 1\r\nOLT#"
    )

    assert result.accepted is True
    assert result.ont_id is None


def test_ont_add_accepted_and_reports_the_assigned_id() -> None:
    result = parse_huawei_ont_add_result(f"{_ADD_ECHO}\r\n  ONTID :0\r\nOLT#")

    assert result.accepted is True
    assert result.ont_id == 0


def test_ont_add_zero_success_count_is_not_acceptance() -> None:
    result = parse_huawei_ont_add_result(
        "  Number of ONTs that can be added: 1, success: 0\r\nOLT#"
    )

    assert result.accepted is False


def test_ont_add_rejection_is_checked_before_any_acceptance_evidence() -> None:
    """A rejection that names an ONT-ID must not read as a success.

    The previous local check ran ``"ont-id" in output`` *before* any error
    test, so this output authorized nothing but reported ONT-ID 5.
    """
    result = parse_huawei_ont_add_result(
        "  Failure: Configuring ONT-ID 5 failed\r\nOLT#"
    )

    assert result.accepted is False
    assert result.ont_id is None


def test_ont_add_echo_alone_is_not_acceptance() -> None:
    """A silent shelf is not a success; the caller must read the state back."""
    result = parse_huawei_ont_add_result(f"{_ADD_ECHO}\r\nOLT(config-if-gpon-0/1)#")

    assert result.accepted is False
    assert result.ont_id is None
    assert result.code == HuaweiCliErrorCode.NONE


def test_ont_add_profile_ids_in_the_echo_are_not_read_as_an_ont_id() -> None:
    result = parse_huawei_ont_add_result(f"{_ADD_ECHO}\r\n  ONTID :7\r\nOLT#")

    assert result.ont_id == 7


def test_typed_outcome_evidence_matches_the_text_classifier_projection() -> None:
    assert project_response_code_evidence(HuaweiCliErrorCode.UNKNOWN_COMMAND) == (
        classify_huawei_cli_response("% Unknown command").to_evidence()
    )
