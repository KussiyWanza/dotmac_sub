"""Typed contracts shared by ONT authorization owners and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID

from app.services.domain_errors import DomainError
from app.services.network.olt_validators import (
    ValidationError,
    validate_fsp,
    validate_serial_number,
)
from app.services.network.serial_utils import canonical as canonical_serial
from app.services.owner_commands import CommandContext


class OntAuthorizationContractError(DomainError):
    """Stable failure raised while converting transport values to domain types."""


def _contract_error(code: str, message: str, **details: object) -> DomainError:
    return OntAuthorizationContractError(
        code=f"network.ont_authorization.{code}",
        message=message,
        details=details,
    )


def _uuid(value: UUID | str, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise _contract_error(
            "invalid_identifier",
            f"{field} must be a UUID.",
            field=field,
        ) from exc


@dataclass(frozen=True, slots=True)
class OntFsp:
    """Validated Huawei frame/slot/port identity."""

    frame: int
    slot: int
    port: int

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            validated = validate_fsp(value)
        except ValidationError as exc:
            raise _contract_error(
                "invalid_fsp",
                exc.message,
                field=exc.field or "fsp",
            ) from exc
        frame, slot, port = (int(part) for part in validated.split("/"))
        return cls(frame=frame, slot=slot, port=port)

    @property
    def value(self) -> str:
        return f"{self.frame}/{self.slot}/{self.port}"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OntSerialNumber:
    """Validated canonical ONT serial identity."""

    value: str

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            validated = validate_serial_number(value)
        except ValidationError as exc:
            raise _contract_error(
                "invalid_serial",
                exc.message,
                field=exc.field or "serial_number",
            ) from exc
        canonical = canonical_serial(validated)
        if not canonical:
            raise _contract_error(
                "invalid_serial",
                "serial_number is required.",
                field="serial_number",
            )
        return cls(canonical)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OntAuthorizationTarget:
    """One exact OLT/F/S/P/serial device target."""

    olt_id: UUID
    fsp: OntFsp
    serial_number: OntSerialNumber

    @classmethod
    def from_transport(
        cls,
        *,
        olt_id: UUID | str,
        fsp: str,
        serial_number: str,
    ) -> Self:
        return cls(
            olt_id=_uuid(olt_id, "olt_id"),
            fsp=OntFsp.parse(fsp),
            serial_number=OntSerialNumber.parse(serial_number),
        )


@dataclass(frozen=True, slots=True)
class RequestAssignedOntAuthorization:
    """Admission command for authorization with an exact active assignment."""

    context: CommandContext
    ont_id: UUID
    target: OntAuthorizationTarget
    force_reauthorize: bool = False
    preset_id: UUID | None = None

    @classmethod
    def from_transport(
        cls,
        *,
        context: CommandContext,
        ont_id: UUID | str,
        olt_id: UUID | str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool = False,
        preset_id: UUID | str | None = None,
    ) -> Self:
        return cls(
            context=context,
            ont_id=_uuid(ont_id, "ont_id"),
            target=OntAuthorizationTarget.from_transport(
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
            ),
            force_reauthorize=force_reauthorize,
            preset_id=_uuid(preset_id, "preset_id") if preset_id is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ExecuteAssignedOntAuthorization:
    """Worker command for the exact assigned authorization admitted earlier."""

    context: CommandContext
    operation_id: UUID
    ont_id: UUID
    target: OntAuthorizationTarget
    force_reauthorize: bool = False
    preset_id: UUID | None = None

    @classmethod
    def from_transport(
        cls,
        *,
        context: CommandContext,
        operation_id: UUID | str,
        ont_id: UUID | str,
        olt_id: UUID | str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool = False,
        preset_id: UUID | str | None = None,
    ) -> Self:
        return cls(
            context=context,
            operation_id=_uuid(operation_id, "operation_id"),
            ont_id=_uuid(ont_id, "ont_id"),
            target=OntAuthorizationTarget.from_transport(
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
            ),
            force_reauthorize=force_reauthorize,
            preset_id=_uuid(preset_id, "preset_id") if preset_id is not None else None,
        )


@dataclass(frozen=True, slots=True)
class RegisterCommissioningOnt:
    """Worker command for management-only registration under an exact intent."""

    context: CommandContext
    operation_id: UUID
    intent_id: UUID
    target: OntAuthorizationTarget


@dataclass(frozen=True, slots=True)
class OntAuthorizationAdmission:
    """Durable acceptance outcome returned to assigned-authorization adapters."""

    accepted: bool
    waiting: bool
    message: str
    operation_id: UUID | None = None
    dispatch_id: UUID | None = None
    duplicate: bool = False


class AssignedAuthorizationDecisionCode(StrEnum):
    """Stable outcomes from exact-assignment authorization admission."""

    ALLOWED = "allowed"
    OLT_NOT_FOUND = "olt_not_found"
    ONT_NOT_FOUND = "ont_not_found"
    SERIAL_MISMATCH = "serial_mismatch"
    ASSIGNMENT_REQUIRED = "assignment_required"
    TOPOLOGY_MISMATCH = "topology_mismatch"


class AuthorizationWorkflowStatus(StrEnum):
    """Stable terminal severity of an authorization workflow."""

    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AssignedAuthorizationDecision:
    """Typed, fail-closed decision for an assigned authorization target."""

    code: AssignedAuthorizationDecisionCode
    message: str

    @property
    def allowed(self) -> bool:
        return self.code is AssignedAuthorizationDecisionCode.ALLOWED
