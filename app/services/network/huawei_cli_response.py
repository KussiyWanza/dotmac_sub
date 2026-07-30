"""Canonical classification for Huawei OLT CLI responses.

Huawei firmware families vary in wording, but callers should consume stable
codes and predicates instead of matching response text themselves. This module
owns that translation. Explicit but unfamiliar failure lines map to a loud
unknown error, while expected absence remains a separate caller decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class HuaweiCliErrorCode(Enum):
    """Stable codes projected from Huawei CLI response text."""

    NONE = "none"
    ALREADY_EXISTS = "already_exists"
    SERIAL_ALREADY_EXISTS = "serial_already_exists"
    NO_AUTOFIND_ENTRIES = "no_autofind_entries"
    VLAN_NOT_EXIST = "vlan_not_exist"
    ONT_OFFLINE = "ont_offline"
    ONT_NOT_EXIST = "ont_not_exist"
    SERVICE_PORT_NOT_EXIST = "service_port_not_exist"
    PROFILE_NOT_EXIST = "profile_not_exist"
    PARAMETER_ERROR = "parameter_error"
    UNKNOWN_COMMAND = "unknown_command"
    PERMISSION_DENIED = "permission_denied"
    RESOURCE_BUSY = "resource_busy"
    INDEX_OUT_OF_RANGE = "index_out_of_range"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    UNKNOWN_ERROR = "unknown_error"


class HuaweiCliResource(Enum):
    """Resources whose absence has operation-specific meaning."""

    ONT = "ont"
    SERVICE_PORT = "service_port"
    PROFILE = "profile"
    VLAN = "vlan"


@dataclass(frozen=True)
class HuaweiCliResponse:
    """Structured interpretation of one Huawei CLI response."""

    output: str
    error_code: HuaweiCliErrorCode
    matched_pattern: str | None = None
    has_error_marker: bool = False

    @property
    def accepted(self) -> bool:
        """Whether the response is a semantic success for a generic command."""
        return self.error_code in {
            HuaweiCliErrorCode.NONE,
            HuaweiCliErrorCode.ALREADY_EXISTS,
            HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES,
        }

    @property
    def is_idempotent_success(self) -> bool:
        return self.error_code == HuaweiCliErrorCode.ALREADY_EXISTS

    @property
    def is_absent(self) -> bool:
        return self.error_code in _ABSENCE_CODES

    @property
    def is_unsupported(self) -> bool:
        return self.error_code == HuaweiCliErrorCode.UNKNOWN_COMMAND

    @property
    def retryable(self) -> bool:
        return self.error_code in {
            HuaweiCliErrorCode.CONNECTION_ERROR,
            HuaweiCliErrorCode.RESOURCE_BUSY,
            HuaweiCliErrorCode.TIMEOUT,
        }

    def to_evidence(self) -> dict[str, object]:
        """Return a JSON-safe, sanitized projection for operation history."""
        return {
            "classifier": "huawei_cli_response",
            "schema_version": 1,
            "response_code": self.error_code.value,
            "accepted": self.accepted,
            "has_error_marker": self.has_error_marker,
            "idempotent_success": self.is_idempotent_success,
            "resource_absent": self.is_absent,
            "unsupported": self.is_unsupported,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class _ResponsePattern:
    pattern: re.Pattern[str]
    code: HuaweiCliErrorCode
    has_error_marker: bool = True


def _pattern(
    expression: str,
    code: HuaweiCliErrorCode,
    *,
    error: bool = True,
) -> _ResponsePattern:
    return _ResponsePattern(
        pattern=re.compile(expression, re.IGNORECASE | re.MULTILINE),
        code=code,
        has_error_marker=error,
    )


def _response_line(body: str) -> str:
    """Match a complete response line with an optional Huawei error envelope."""
    return (
        r"^\s*(?:olt\s+(?:rejected|error)\s*:\s*)?"
        rf"(?:(?:failure|error)\s*:\s*)?(?:{body})\s*\.?\s*$"
    )


def _error_line(body: str) -> str:
    """Match a complete response line that explicitly reports an error."""
    return (
        r"^\s*(?:olt\s+(?:rejected|error)\s*:\s*)?"
        rf"(?:(?:failure|error)\s*:\s*|%\s*)(?:{body})\s*\.?\s*$"
    )


# First match wins. Specific semantic outcomes must precede generic failures.
_RESPONSE_PATTERNS = (
    _pattern(
        _response_line(r"automatically\s+found\s+onts?\s+(?:do|does)\s+not\s+exist"),
        HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES,
        error=False,
    ),
    _pattern(
        _response_line(r"(?:sn|serial(?:\s+number)?)\s+already\s+exists"),
        HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?service\s+virtual\s+port\s+has\s+existed\s+already"
        ),
        HuaweiCliErrorCode.ALREADY_EXISTS,
    ),
    _pattern(
        _response_line(r".*conflicted\s+service\s+virtual\s+port\s+index\s*:?\s*\d+"),
        HuaweiCliErrorCode.ALREADY_EXISTS,
    ),
    _pattern(
        _response_line(r"tr069.*server.*profile.*already.*bindw.*"),
        HuaweiCliErrorCode.ALREADY_EXISTS,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?(?:resource|profile|vlan|ont|service[-\s]+port|tr069.*profile)"
            r"\b.*\balready\s+exists\b.*"
        ),
        HuaweiCliErrorCode.ALREADY_EXISTS,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?service(?:\s+virtual)?[-\s]+port"
            r"\s+(?:does|do|is)\s+not\s+exist"
        ),
        HuaweiCliErrorCode.SERVICE_PORT_NOT_EXIST,
    ),
    _pattern(
        _response_line(
            r"service(?:\s+virtual)?[-\s]+port(?:\s+\d+)?"
            r"\s+(?:was\s+)?not\s+found"
        ),
        HuaweiCliErrorCode.SERVICE_PORT_NOT_EXIST,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?vlan.*"
            r"(?:does\s+not\s+exist|is\s+not\s+exist|not\s+configured)"
        ),
        HuaweiCliErrorCode.VLAN_NOT_EXIST,
    ),
    _pattern(
        _response_line(r"(?:the\s+)?ont(?:\s+\d+)?\s+(?:is\s+not\s+online|offline)"),
        HuaweiCliErrorCode.ONT_OFFLINE,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?(?:required\s+)?ont(?:\s+\d+)?\s+"
            r"(?:does\s+not\s+exist|is\s+not\s+exist|not\s+found)"
            r"|unknown\s+ont(?:\s+\d+)?"
        ),
        HuaweiCliErrorCode.ONT_NOT_EXIST,
    ),
    _pattern(
        _response_line(
            r"(?:the\s+)?(?:tr069\s+server\s+)?profile(?:\s+\d+)?\s+"
            r"(?:does\s+not\s+exist|is\s+not\s+exist|not\s+found)"
        ),
        HuaweiCliErrorCode.PROFILE_NOT_EXIST,
    ),
    _pattern(
        _error_line(
            r"(?:index.*out\s+of\s+range|.*exceeds.*maximum|ip-index.*invalid)"
        ),
        HuaweiCliErrorCode.INDEX_OUT_OF_RANGE,
    ),
    _pattern(
        _error_line(r"(?:parameter\s+error.*|invalid\s+(?:parameter|input).*)"),
        HuaweiCliErrorCode.PARAMETER_ERROR,
    ),
    _pattern(
        _error_line(
            r"(?:unknown\s+command.*|command\s+not\s+found|incomplete\s+command.*|unrecognized.*)"
        ),
        HuaweiCliErrorCode.UNKNOWN_COMMAND,
    ),
    _pattern(
        _error_line(
            r"(?:permission\s+denied|access\s+denied|insufficient\s+privilege.*)"
        ),
        HuaweiCliErrorCode.PERMISSION_DENIED,
    ),
    _pattern(
        _error_line(r"(?:resource.*busy|.*\blocked\b.*)"),
        HuaweiCliErrorCode.RESOURCE_BUSY,
    ),
    _pattern(r"\u5931\u8d25|\u9519\u8bef", HuaweiCliErrorCode.UNKNOWN_ERROR),
    _pattern(
        r"^\s*(?:olt\s+(?:rejected|error)\s*:\s*)?(?:%\s*)?"
        r"(?:failure|failed|error)\b\s*[:.]?",
        HuaweiCliErrorCode.UNKNOWN_ERROR,
    ),
)

_ABSENCE_CODES = {
    HuaweiCliErrorCode.VLAN_NOT_EXIST,
    HuaweiCliErrorCode.ONT_NOT_EXIST,
    HuaweiCliErrorCode.SERVICE_PORT_NOT_EXIST,
    HuaweiCliErrorCode.PROFILE_NOT_EXIST,
}

_RESOURCE_ABSENCE_CODES = {
    HuaweiCliResource.ONT: {HuaweiCliErrorCode.ONT_NOT_EXIST},
    HuaweiCliResource.SERVICE_PORT: {HuaweiCliErrorCode.SERVICE_PORT_NOT_EXIST},
    HuaweiCliResource.PROFILE: {HuaweiCliErrorCode.PROFILE_NOT_EXIST},
    HuaweiCliResource.VLAN: {HuaweiCliErrorCode.VLAN_NOT_EXIST},
}


def classify_huawei_cli_response(output: object) -> HuaweiCliResponse:
    """Classify raw or wrapped Huawei CLI text into a stable response code."""
    text = str(output or "")
    for candidate in _RESPONSE_PATTERNS:
        if candidate.pattern.search(text):
            return HuaweiCliResponse(
                output=text,
                error_code=candidate.code,
                matched_pattern=candidate.pattern.pattern,
                has_error_marker=candidate.has_error_marker,
            )
    return HuaweiCliResponse(output=text, error_code=HuaweiCliErrorCode.NONE)


def project_huawei_result_evidence(result: object) -> dict[str, object] | None:
    """Project sanitized classifier and transport codes from an adapter result."""
    evidence: dict[str, object] = {}
    error_code = getattr(result, "error_code", None)
    if error_code:
        evidence["error_code"] = str(error_code)
    result_data = getattr(result, "data", None)
    if isinstance(result_data, dict) and isinstance(
        result_data.get("huawei_cli_response"), dict
    ):
        evidence["huawei_cli_response"] = dict(result_data["huawei_cli_response"])
    return evidence or None


def has_huawei_cli_error(output: object) -> bool:
    """Return whether Huawei reported a command error or conflict marker."""
    return classify_huawei_cli_response(output).has_error_marker


def is_huawei_resource_absent(
    output: object,
    resource: HuaweiCliResource,
) -> bool:
    """Return whether the response specifically reports ``resource`` absent."""
    response = classify_huawei_cli_response(output)
    return response.error_code in _RESOURCE_ABSENCE_CODES[resource]


def is_huawei_cli_unsupported(
    output: object,
    *,
    parameter_error_is_unsupported: bool = False,
) -> bool:
    """Return whether this firmware rejected the command grammar."""
    response = classify_huawei_cli_response(output)
    if response.is_unsupported:
        return True
    return (
        parameter_error_is_unsupported
        and response.error_code == HuaweiCliErrorCode.PARAMETER_ERROR
    )


def is_huawei_idempotent_conflict(output: object) -> bool:
    return (
        classify_huawei_cli_response(output).error_code
        == HuaweiCliErrorCode.ALREADY_EXISTS
    )


def is_huawei_ont_offline(output: object) -> bool:
    return (
        classify_huawei_cli_response(output).error_code
        == HuaweiCliErrorCode.ONT_OFFLINE
    )


def is_huawei_serial_already_registered(output: object) -> bool:
    return (
        classify_huawei_cli_response(output).error_code
        == HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS
    )


def is_huawei_no_autofind_entries(output: object) -> bool:
    return (
        classify_huawei_cli_response(output).error_code
        == HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES
    )
