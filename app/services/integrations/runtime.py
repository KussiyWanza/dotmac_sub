"""Connector runtime contracts without provider or persistence authority."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.integrations.diagnostics import OperationDiagnostic
from app.services.integrations.manifest import ConnectorManifest


class OperationTrigger(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    event = "event"
    inbound = "inbound"
    interactive = "interactive"
    reconcile = "reconcile"


class OperationStatus(StrEnum):
    succeeded = "succeeded"
    partial = "partial"
    retryable = "retryable"
    rejected = "rejected"
    reconciliation_required = "reconciliation_required"
    failed = "failed"
    canceled = "canceled"


class OperationEnvelope(BaseModel):
    """Version-pinned transport envelope for one typed capability call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    correlation_id: str = Field(min_length=1, max_length=160)
    installation_id: UUID
    capability_binding_id: UUID
    capability_id: str
    connector_key: str
    connector_version: str
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_revision_id: UUID
    trigger: OperationTrigger
    idempotency_key: str = Field(min_length=1, max_length=240)
    deadline_at: datetime
    payload: dict[str, Any]
    actor: str | None = Field(default=None, max_length=160)


class OperationResult(BaseModel):
    """Sanitized connector outcome; domain owners decide its consequence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    status: OperationStatus
    output: dict[str, Any] = Field(default_factory=dict)
    diagnostic: OperationDiagnostic | None = None
    external_receipt: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=120)
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86_400)


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    error_codes: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConnectorRunner(Protocol):
    """Runtime transport only; implementations receive no Sub DB session."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult: ...

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult: ...

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult: ...

    def cancel(self, operation_id: UUID) -> bool: ...


@runtime_checkable
class CapabilityValidationRunner(Protocol):
    """Runner that can validate the exact capability being enabled."""

    def validate_capability(
        self,
        *,
        capability_id: str,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult: ...


class RunnerRegistry:
    """Process-local runner resolver populated explicitly at worker startup."""

    def __init__(self) -> None:
        self._runners: dict[str, ConnectorRunner] = {}

    def register(self, connector_key: str, runner: ConnectorRunner) -> None:
        key = connector_key.strip().lower()
        if not key:
            raise ValueError("connector key is required")
        if key in self._runners:
            raise ValueError(f"runner already registered for {key}")
        self._runners[key] = runner

    def resolve(self, connector_key: str) -> ConnectorRunner:
        key = connector_key.strip().lower()
        try:
            return self._runners[key]
        except KeyError as exc:
            raise LookupError(f"no runner registered for {key}") from exc

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))
