"""Application side of the external connector runtime tier.

Phase 3 of ADR 0005, marshalling half. ``ExternalOciRunner`` implements the
in-process ``ConnectorRunner`` protocol by translating each verb into a
``RunnerRequest``, handing it to a ``RunnerTransport`` that carries it to an
out-of-process connector container, and translating the ``RunnerResponse``
back. The runner owns no business decision: it is a transport adapter around
the wire contract, and every consequence of what a connector observes still
belongs to the domain owner that receives the returned ``OperationResult``.

The Podman-specific transport is deliberately a separate, swappable unit
(installed only where a runtime is configured). This module and its tests are
exercised entirely with an in-memory transport, so the marshalling — including
the security and failure semantics below — is verified without a container
runtime or prod.

Two behaviours are load-bearing and pinned by tests:

- **Secret material never enters the request.** ``RunnerRequest`` has no field
  for it; credentials are handed to the transport out of band for exactly one
  binding, so a serialized request stays safe to log and replay.
- **An ambiguous timeout is not retried.** A transport timeout on ``execute``
  becomes ``reconciliation_required`` — the design forbids blindly repeating an
  operation whose remote outcome is unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.services.integrations.manifest import ConnectorManifest, ConnectorRuntimeType
from app.services.integrations.runner_protocol import (
    ConnectorPin,
    RunnerRequest,
    RunnerResponse,
    RunnerVerb,
)
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 600


class RunnerTransportError(RuntimeError):
    """The transport could not obtain a trustworthy response from the container.

    Covers a container that never produced usable output — a crash, a non-zero
    exit, unreadable or malformed bytes. Nothing here is ever re-raised into
    caller code: a semi-trusted external connector must not be able to crash
    Sub, so the runner maps every failure to a fail-closed typed result.
    """


class RunnerTimeout(RunnerTransportError):
    """The container did not answer within the operation deadline."""


class RunnerProtocolError(RunnerTransportError):
    """The container answered, but the answer violates the wire contract.

    A response for the wrong verb or a different operation id. Distinct from a
    plain transport error because the container *did* respond and may have acted
    on the request, which makes an execute outcome ambiguous rather than safely
    retryable.
    """


class RunnerTransport(Protocol):
    """Carries one request to a connector container and returns its response.

    An implementation launches the digest-pinned image, delivers
    ``secret_material`` out of band (never argv, never the image, never the
    request), writes the request, reads and validates the response, enforces
    ``deadline_at`` by killing the container, and tears it down. It raises
    ``RunnerTimeout`` at the deadline and ``RunnerTransportError`` on any other
    failure to obtain a well-formed response.
    """

    def exchange(
        self,
        *,
        request: RunnerRequest,
        image_ref: str,
        secret_material: Mapping[str, str],
        deadline_at: datetime,
    ) -> RunnerResponse: ...


class ExternalOciRunner:
    """Run a connector's operations out of process over a ``RunnerTransport``.

    Constructed per connector with its manifest, mirroring how in-process
    runners are registered per connector. The manifest fixes the image and
    digest to launch and the pin that travels with every request.
    """

    def __init__(self, manifest: ConnectorManifest, transport: RunnerTransport) -> None:
        if manifest.runtime.type is not ConnectorRuntimeType.external_oci:
            raise ValueError(
                f"connector {manifest.key!r} is not an external_oci connector"
            )
        if not manifest.runtime.image or not manifest.runtime.digest:
            raise ValueError(
                f"connector {manifest.key!r} does not pin an image and digest"
            )
        self._manifest = manifest
        self._transport = transport
        self._pin = ConnectorPin.from_manifest(manifest)

    @property
    def _image_ref(self) -> str:
        return f"{self._manifest.runtime.image}@{self._manifest.runtime.digest}"

    def _require_same_connector(self, manifest: ConnectorManifest) -> None:
        if not self._pin.matches(manifest):
            raise RunnerTransportError(
                f"runner is pinned to {self._manifest.key} "
                f"{self._manifest.version}/{self._manifest.digest[:12]}, "
                f"asked to run {manifest.key} {manifest.version}"
            )

    def _timeout_seconds(self, config: Mapping[str, Any]) -> int:
        raw = config.get("timeout_seconds")
        try:
            value = int(raw) if raw is not None else _DEFAULT_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            value = _DEFAULT_TIMEOUT_SECONDS
        return max(1, min(value, _MAX_TIMEOUT_SECONDS))

    def _deadline_from_config(self, config: Mapping[str, Any]) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self._timeout_seconds(config))

    def _exchange(
        self,
        *,
        request: RunnerRequest,
        secret_material: Mapping[str, str],
        deadline_at: datetime,
    ) -> RunnerResponse:
        response = self._transport.exchange(
            request=request,
            image_ref=self._image_ref,
            secret_material=secret_material,
            deadline_at=deadline_at,
        )
        if response.verb is not request.verb:
            raise RunnerProtocolError(
                f"connector answered {response.verb.value} to a "
                f"{request.verb.value} request"
            )
        return response

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        self._require_same_connector(manifest)
        request = RunnerRequest(
            verb=RunnerVerb.validate,
            connector=self._pin,
            config=dict(config),
        )
        try:
            response = self._exchange(
                request=request,
                secret_material=secret_material,
                deadline_at=self._deadline_from_config(config),
            )
        except RunnerTimeout:
            return ValidationResult(valid=False, error_codes=("runner_timeout",))
        except RunnerTransportError as exc:
            return ValidationResult(
                valid=False,
                error_codes=("runner_transport_error",),
                details={"message": str(exc)},
            )
        assert response.validation is not None  # guaranteed by response schema
        return response.validation

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        request = RunnerRequest(
            verb=RunnerVerb.execute,
            connector=self._pin,
            config=dict(config),
            envelope=envelope,
        )
        try:
            response = self._exchange(
                request=request,
                secret_material=secret_material,
                deadline_at=envelope.deadline_at,
            )
            assert response.operation is not None  # guaranteed by response schema
            result = response.operation
            if result.operation_id != envelope.operation_id:
                raise RunnerProtocolError(
                    "connector answered a different operation id than was sent"
                )
        except (RunnerTimeout, RunnerProtocolError) as exc:
            # The container responded (or timed out mid-flight) and may have
            # acted, so the remote outcome is unknown. Hand it to reconciliation
            # rather than retrying or declaring failure — never blindly repeat.
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.reconciliation_required,
                error_code=(
                    "runner_timeout"
                    if isinstance(exc, RunnerTimeout)
                    else "runner_protocol_error"
                ),
            )
        except RunnerTransportError as exc:
            # No usable response at all; assume the operation never ran.
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                error_code="runner_transport_error",
                output={"message": str(exc)},
            )
        return result

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        self._require_same_connector(manifest)
        request = RunnerRequest(
            verb=RunnerVerb.health,
            connector=self._pin,
            config=dict(config),
        )
        try:
            response = self._exchange(
                request=request,
                secret_material=secret_material,
                deadline_at=self._deadline_from_config(config),
            )
        except RunnerTransportError as exc:
            status = "unknown" if isinstance(exc, RunnerTimeout) else "unavailable"
            return HealthResult(status=status, details={"message": str(exc)})
        assert response.health is not None  # guaranteed by response schema
        return response.health

    def cancel(self, operation_id: UUID) -> bool:
        request = RunnerRequest(
            verb=RunnerVerb.cancel,
            connector=self._pin,
            operation_id=operation_id,
        )
        try:
            response = self._exchange(
                request=request,
                secret_material={},
                deadline_at=datetime.now(UTC)
                + timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS),
            )
        except RunnerTransportError:
            return False
        return bool(response.canceled)


__all__ = [
    "ExternalOciRunner",
    "RunnerProtocolError",
    "RunnerTimeout",
    "RunnerTransport",
    "RunnerTransportError",
]
