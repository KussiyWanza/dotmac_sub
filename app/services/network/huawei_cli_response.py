"""Canonical classification for Huawei OLT CLI responses.

Huawei firmware families vary in wording, but callers should consume stable
codes and predicates instead of matching response text themselves. This module
owns that translation. Explicit but unfamiliar failure lines map to a loud
unknown error, while expected absence remains a separate caller decision.

Ownership rule
--------------
Classification happens **once, on raw device output, at the point of capture**.
The result travels as :class:`HuaweiDeviceOutcome` — a typed, deeply immutable
carrier. Downstream callers read ``outcome.code``; they must never re-classify
``outcome.message``, because that string is operator-facing, truncated, and
wrapped. Re-parsing it silently disabled the duplicate-serial recovery branch in
``ont_authorization`` while every synthetic-message unit test kept passing.

Operator-facing rejection text is also owned here
(:func:`describe_huawei_rejection`) so the envelope this module can parse and
the envelope the stack emits cannot drift apart again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


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


# The stack wraps device output for operators before it is stored or shown.
# Historically each call site invented its own prefix ("OLT rejected:",
# "OLT rejected command:", "OLT rejected upgrade:", "OLT rejected '<cmd>':"),
# and only the bare form was parseable — so a wrapped authorize rejection
# classified as NONE. The envelope now tolerates any short qualifier up to the
# first colon, and :func:`describe_huawei_rejection` emits the canonical form.
_REJECTION_ENVELOPE = r"(?:olt\s+(?:rejected|error)[^:\r\n]{0,48}:\s*)?"


def _response_line(body: str) -> str:
    """Match a complete response line with an optional Huawei error envelope."""
    return (
        rf"^\s*{_REJECTION_ENVELOPE}"
        rf"(?:(?:failure|error)\s*:\s*)?(?:{body})\s*\.?\s*$"
    )


def _error_line(body: str) -> str:
    """Match a complete response line that explicitly reports an error."""
    return (
        rf"^\s*{_REJECTION_ENVELOPE}"
        rf"(?:(?:failure|error)\s*:\s*|%\s*)(?:{body})\s*\.?\s*$"
    )


# First match wins. Specific semantic outcomes must precede generic failures.
_RESPONSE_PATTERNS = (
    # BOI and Gudu return "Failure: The automatically found ONTs do not exist"
    # for an empty autofind table. The leading article is mandatory on those
    # builds; omitting ``(?:the\s+)?`` here made an authoritative empty result
    # classify as UNKNOWN_ERROR and surface as "Autofind query failed".
    _pattern(
        _response_line(
            r"(?:the\s+)?automatically\s+found\s+onts?\s+(?:do|does)\s+not\s+exist"
        ),
        HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES,
        error=False,
    ),
    # Duplicate-serial rejections are the trigger for the reuse/move recovery
    # branch in ``ont_authorization``, so they must be recognised across
    # firmware wording. The strict complete-line form stays first; the looser
    # forms require an explicit failure envelope so a ``display`` row that
    # merely mentions a serial can never be mistaken for a conflict.
    _pattern(
        _response_line(r"(?:sn|serial(?:\s+number)?)\s+already\s+exists"),
        HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS,
    ),
    _pattern(
        _error_line(
            r"[^\r\n]*\b(?:sn|serial(?:\s+number)?)\b[^\r\n]*?"
            r"\balready\s+exists\b[^\r\n]*"
        ),
        HuaweiCliErrorCode.SERIAL_ALREADY_EXISTS,
    ),
    _pattern(
        _error_line(
            r"[^\r\n]*\b(?:sn|serial(?:\s+number)?)\b[^\r\n]*?\b(?:"
            r"has\s+(?:already\s+)?been\s+used"
            r"|is\s+(?:already\s+)?(?:in\s+use|used|duplicated?|conflicted)"
            r"|is\s+conflicted\s+with"
            r")\b[^\r\n]*"
        ),
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


#: Codes that describe an expected outcome rather than a device error.
_NON_ERROR_CODES = frozenset(
    {HuaweiCliErrorCode.NONE, HuaweiCliErrorCode.NO_AUTOFIND_ENTRIES}
)


def project_response_code_evidence(code: HuaweiCliErrorCode) -> dict[str, object]:
    """Sanitized evidence for an already-typed device verdict.

    Lets callers that carry a code (rather than raw text) record the same
    operation-history projection as :meth:`HuaweiCliResponse.to_evidence`
    without re-parsing a message.
    """
    return HuaweiCliResponse(
        output="",
        error_code=code,
        has_error_marker=code not in _NON_ERROR_CODES,
    ).to_evidence()


def describe_huawei_rejection(
    output: object,
    *,
    action: str | None = None,
    code: HuaweiCliErrorCode | None = None,
    detail_limit: int = 200,
) -> str:
    """Build the one canonical operator-facing rejection message.

    Every rejection the OLT stack shows or stores goes through here so the
    envelope this module can parse and the envelope the stack emits stay the
    same string. ``action`` and ``code`` are appended *after* the device text,
    never inside the envelope, so the classifier still sees the device line
    intact and can recover the verdict from the wrapped message.
    """
    detail = str(output or "").strip()
    if detail_limit > 0:
        detail = detail[-detail_limit:]
    annotations = [part for part in (action, code.value if code else None) if part]
    suffix = f" ({': '.join(annotations)})" if annotations else ""
    return f"OLT rejected: {detail}{suffix}" if detail else f"OLT rejected{suffix}"


@dataclass(frozen=True, slots=True)
class HuaweiDeviceOutcome:
    """Typed, deeply immutable outcome of one Huawei CLI exchange.

    Built once from raw device output at the point of capture and passed
    downstream unchanged. Consumers branch on :attr:`code`; nothing may
    re-classify :attr:`message`, which is truncated and operator-facing.
    """

    SCHEMA_VERSION: ClassVar[int] = 1

    succeeded: bool
    code: HuaweiCliErrorCode
    message: str
    device_detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.succeeded

    @property
    def retryable(self) -> bool:
        return self.code in {
            HuaweiCliErrorCode.CONNECTION_ERROR,
            HuaweiCliErrorCode.RESOURCE_BUSY,
            HuaweiCliErrorCode.TIMEOUT,
        }

    @classmethod
    def accepted(
        cls,
        message: str,
        *,
        code: HuaweiCliErrorCode = HuaweiCliErrorCode.NONE,
        device_detail: str = "",
    ) -> HuaweiDeviceOutcome:
        """Record a semantic success, optionally an idempotent one."""
        return cls(
            succeeded=True,
            code=code,
            message=message,
            device_detail=device_detail,
        )

    @classmethod
    def rejected_by_device(
        cls,
        output: object,
        *,
        action: str | None = None,
        detail_limit: int = 200,
    ) -> HuaweiDeviceOutcome:
        """Classify raw device output and wrap it for operators, once."""
        response = classify_huawei_cli_response(output)
        detail = str(output or "").strip()
        return cls(
            succeeded=False,
            code=(
                response.error_code
                if response.error_code is not HuaweiCliErrorCode.NONE
                else HuaweiCliErrorCode.UNKNOWN_ERROR
            ),
            message=describe_huawei_rejection(
                detail, action=action, detail_limit=detail_limit
            ),
            device_detail=detail[-detail_limit:] if detail_limit > 0 else detail,
        )

    @classmethod
    def transport_failure(
        cls,
        message: str,
        *,
        code: HuaweiCliErrorCode = HuaweiCliErrorCode.CONNECTION_ERROR,
    ) -> HuaweiDeviceOutcome:
        """Record a failure that never reached the device grammar."""
        return cls(succeeded=False, code=code, message=message)

    def to_evidence(self) -> dict[str, object]:
        """Return a JSON-safe, sanitized projection for operation history."""
        return {
            "classifier": "huawei_device_outcome",
            "schema_version": self.SCHEMA_VERSION,
            "response_code": self.code.value,
            "succeeded": self.succeeded,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class HuaweiOntAddResult:
    """Typed reading of a Huawei ``ont add ... sn-auth ...`` response."""

    accepted: bool
    ont_id: int | None
    code: HuaweiCliErrorCode


#: ``Number of ONTs that can be added: 1, success: 1`` (MA5600T/MA5800) and
#: ``ONTID :0`` / ``ONT ID : 0`` (MA5608T) both report acceptance. A zero count
#: is not acceptance, so the success counter must be non-zero.
_ONT_ADD_SUCCESS_COUNT_RE = re.compile(
    r"\bsuccess(?:ful|fully)?\s*[:=]?\s*([1-9]\d*)\b", re.IGNORECASE
)
#: ``ont-lineprofile-id``/``ont-srvprofile-id`` in the echoed command must not
#: match, so ``id`` has to follow ``ont`` directly.
_ONT_ADD_ID_RE = re.compile(r"\bont\s*-?\s*id\b\D{0,8}?(\d+)", re.IGNORECASE)
#: The shell echoes the command back before responding; that line is input,
#: not a device verdict.
_ONT_ADD_ECHO_RE = re.compile(r"\bsn-auth\b", re.IGNORECASE)


def parse_huawei_ont_add_result(output: object) -> HuaweiOntAddResult:
    """Interpret an ``ont add`` response as acceptance plus assigned ONT-ID.

    The error classification is consulted **first**. The previous local check
    (``"success" in output or "ont-id" in output``) ran before any error test,
    so a rejection that happened to name an ONT-ID could be read as a
    successful authorization.
    """
    response = classify_huawei_cli_response(output)
    device_lines = [
        line
        for line in str(output or "").splitlines()
        if not _ONT_ADD_ECHO_RE.search(line)
    ]
    body = "\n".join(device_lines)

    ont_id: int | None = None
    if id_match := _ONT_ADD_ID_RE.search(body):
        ont_id = int(id_match.group(1))

    if response.has_error_marker:
        return HuaweiOntAddResult(accepted=False, ont_id=None, code=response.error_code)

    accepted = bool(_ONT_ADD_SUCCESS_COUNT_RE.search(body)) or ont_id is not None
    return HuaweiOntAddResult(
        accepted=accepted, ont_id=ont_id, code=response.error_code
    )


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
