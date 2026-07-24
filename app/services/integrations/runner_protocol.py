"""Versioned wire contract between Sub and an out-of-process connector runner.

Phase 2 of ADR 0005. This module defines *what crosses the boundary*, not how
it is transported: Phase 3 marshals these models over a concrete channel, and
the same schema is what a third-party connector image implements.

Three properties are deliberate and are enforced by tests rather than asserted.

**Secret material is not on the wire.** ``RunnerRequest`` has no field for it.
An external runner receives credentials out of band for exactly one
installation binding, so secret values cannot leak into a request that is
otherwise safe to log, persist as delivery evidence, or replay. The design
requires that secret values never enter the operation payload, task arguments,
logs, traces, audit rows, or runner artifacts.

**The connector is pinned, not named.** Every request carries the connector
key, version, and sha256 manifest digest. A runner that receives a digest it
was not built for must refuse rather than best-effort interpret the payload.

**Refusal is typed.** A runner answers every verb with a ``RunnerResponse``.
Transport failure is the caller's concern; a runner that reached a decision
reports it in-band so the caller can distinguish "the connector rejected this"
from "the connector never ran".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    ValidationResult,
)

# Final so the type checker infers the Literal the model fields declare,
# rather than a plain str that cannot satisfy them.
RUNNER_CONTRACT_VERSION: Final = "dotmac.io/integrations/runner/v1"


class RunnerVerb(StrEnum):
    """The four operations a connector runtime exposes."""

    validate = "validate"
    execute = "execute"
    health = "health"
    cancel = "cancel"


class ConnectorPin(BaseModel):
    """Identity a runner must match before interpreting a request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_manifest(cls, manifest: ConnectorManifest) -> ConnectorPin:
        return cls(
            key=manifest.key,
            version=manifest.version,
            manifest_digest=manifest.digest,
        )

    def matches(self, manifest: ConnectorManifest) -> bool:
        return (
            self.key == manifest.key
            and self.version == manifest.version
            and self.manifest_digest == manifest.digest
        )

    def matches_envelope(self, envelope: OperationEnvelope) -> bool:
        return (
            self.key == envelope.connector_key
            and self.version == envelope.connector_version
            and self.manifest_digest == envelope.manifest_digest
        )


class RunnerRequest(BaseModel):
    """One verb invocation crossing the boundary.

    Carries no secret material by construction. `config` is the installation's
    non-secret configuration revision; credentials are delivered separately by
    the transport for exactly one capability binding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["dotmac.io/integrations/runner/v1"] = (
        RUNNER_CONTRACT_VERSION
    )
    verb: RunnerVerb
    connector: ConnectorPin
    config: dict[str, Any] = Field(default_factory=dict)
    envelope: OperationEnvelope | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def validate_verb_payload(self) -> RunnerRequest:
        if self.verb is RunnerVerb.execute:
            if self.envelope is None:
                raise ValueError("execute requires an operation envelope")
            if not self.connector.matches_envelope(self.envelope):
                raise ValueError("envelope does not match the pinned connector")
        elif self.envelope is not None:
            raise ValueError(f"{self.verb.value} does not take an operation envelope")

        if self.verb is RunnerVerb.cancel:
            if self.operation_id is None:
                raise ValueError("cancel requires an operation id")
        elif self.operation_id is not None:
            raise ValueError(f"{self.verb.value} does not take an operation id")
        return self


class RunnerResponse(BaseModel):
    """A runner's in-band answer to one request.

    Exactly one result field is populated, matching the requested verb. A
    runner that could not decide raises at the transport layer instead of
    returning an empty response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["dotmac.io/integrations/runner/v1"] = (
        RUNNER_CONTRACT_VERSION
    )
    verb: RunnerVerb
    validation: ValidationResult | None = None
    operation: OperationResult | None = None
    health: HealthResult | None = None
    canceled: bool | None = None

    @model_validator(mode="after")
    def validate_result_matches_verb(self) -> RunnerResponse:
        populated = {
            "validation": self.validation is not None,
            "operation": self.operation is not None,
            "health": self.health is not None,
            "canceled": self.canceled is not None,
        }
        expected = {
            RunnerVerb.validate: "validation",
            RunnerVerb.execute: "operation",
            RunnerVerb.health: "health",
            RunnerVerb.cancel: "canceled",
        }[self.verb]
        if not populated[expected]:
            raise ValueError(f"{self.verb.value} response requires {expected}")
        extra = sorted(
            name for name, is_set in populated.items() if is_set and name != expected
        )
        if extra:
            raise ValueError(
                f"{self.verb.value} response must not carry: {', '.join(extra)}"
            )
        return self


__all__ = [
    "RUNNER_CONTRACT_VERSION",
    "ConnectorPin",
    "RunnerRequest",
    "RunnerResponse",
    "RunnerVerb",
]
