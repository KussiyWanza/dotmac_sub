from __future__ import annotations

import pytest

from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiCliResource,
    classify_huawei_cli_response,
    has_huawei_cli_error,
    is_huawei_cli_unsupported,
    is_huawei_no_autofind_entries,
    is_huawei_resource_absent,
    is_huawei_serial_already_registered,
    project_huawei_result_evidence,
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
