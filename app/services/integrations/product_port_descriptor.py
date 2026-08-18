"""Sub-owned declaration of the Integrator product-port destination.

The Integrator transports this declaration; it does not author it. The binding
UUID is a live Sub identity, while capability meaning and destination scope are
Sub-owned facts. Publishing them together prevents an assembly from permanently
transcribing two independent sources into configuration.

This service is read-only. A named reconciler stores the immutable snapshot and
compares ``descriptor_digest`` on refresh; drift repair never depends on an
event that might not arrive.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.services.domain_errors import DomainError
from app.services.integrations.connectors.integrator_http import (
    INTEGRATOR_CONNECTOR_KEY,
    INTEGRATOR_RECEIVE_CAPABILITY,
)

DESCRIPTOR_SCHEMA_VERSION = "dotmac.io/product-port-descriptor/v1"
APPLICATION = "sub"
OWNER_MODULE = "communications.team_inbox_integrator_envelope"
CAPABILITY_SUMMARY = "Inbound provider message and delivery-state observations"
CONTRACT_VERSION = 1


class ProductPortDescriptorError(DomainError):
    """The requested row is not Sub's declared Integrator product port."""


class ProductPortActivationState(StrEnum):
    CONFIGURED_DISABLED = "configured_disabled"
    ENABLED = "enabled"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class DestinationScope:
    """Sub's opaque local name for this stream; transports never interpret it."""

    kind: str
    ref: str


@dataclass(frozen=True, slots=True)
class ProductPortDescriptorV1:
    schema_version: str
    application: str
    owner_module: str
    capability_id: str
    capability_summary: str
    contract_version: int
    destination_binding_id: UUID
    delivery_path: str
    mirror_path: str
    destination_scope: DestinationScope
    activation_state: str
    source_revision: str
    descriptor_digest: str


DESTINATION_SCOPE = DestinationScope(kind="inbox", ref="support")


def descriptor_digest(document: Mapping[str, object]) -> str:
    """Canonical SHA-256 over every published descriptor field."""

    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _activation_state(
    binding: IntegrationCapabilityBinding,
) -> ProductPortActivationState:
    installation = binding.installation
    if installation.state == IntegrationInstallationState.retired.value:
        return ProductPortActivationState.RETIRED
    if installation.state == IntegrationInstallationState.quarantined.value:
        return ProductPortActivationState.QUARANTINED
    if (
        installation.state == IntegrationInstallationState.enabled.value
        and binding.state == IntegrationBindingState.enabled.value
    ):
        return ProductPortActivationState.ENABLED
    return ProductPortActivationState.CONFIGURED_DISABLED


def product_port_descriptor(
    db: Session, destination_binding_id: UUID
) -> ProductPortDescriptorV1:
    """Return Sub's descriptor for one exact destination binding.

    Configured-but-disabled remains readable because importing the descriptor is
    a prerequisite for enabling delivery. Quarantined and retired bindings stay
    describable so the reconciler can report the actual drift state.
    """

    binding = db.get(IntegrationCapabilityBinding, destination_binding_id)
    if binding is None:
        raise ProductPortDescriptorError(
            code="communications.product_port_descriptor.not_found",
            message="Product port binding not found.",
        )
    installation = binding.installation
    if (
        installation.connector_key != INTEGRATOR_CONNECTOR_KEY
        or binding.capability_id != INTEGRATOR_RECEIVE_CAPABILITY
    ):
        raise ProductPortDescriptorError(
            code="communications.product_port_descriptor.not_found",
            message="Product port binding not found.",
        )

    activation_state = _activation_state(binding)
    source_revision = descriptor_digest(
        {
            "binding_id": binding.id,
            "binding_state": binding.state,
            "binding_updated_at": binding.updated_at,
            "installation_id": installation.id,
            "installation_state": installation.state,
            "installation_updated_at": installation.updated_at,
            "capability_id": INTEGRATOR_RECEIVE_CAPABILITY,
            "capability_summary": CAPABILITY_SUMMARY,
            "contract_version": CONTRACT_VERSION,
            "destination_scope": {
                "kind": DESTINATION_SCOPE.kind,
                "ref": DESTINATION_SCOPE.ref,
            },
        }
    )
    delivery_path = f"/api/v1/integration/observations/{binding.id}"
    mirror_path = f"{delivery_path}/mirror"
    published: dict[str, object] = {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "application": APPLICATION,
        "owner_module": OWNER_MODULE,
        "capability_id": INTEGRATOR_RECEIVE_CAPABILITY,
        "capability_summary": CAPABILITY_SUMMARY,
        "contract_version": CONTRACT_VERSION,
        "destination_binding_id": binding.id,
        "delivery_path": delivery_path,
        "mirror_path": mirror_path,
        "destination_scope": {
            "kind": DESTINATION_SCOPE.kind,
            "ref": DESTINATION_SCOPE.ref,
        },
        "activation_state": activation_state.value,
        "source_revision": source_revision,
    }
    return ProductPortDescriptorV1(
        schema_version=DESCRIPTOR_SCHEMA_VERSION,
        application=APPLICATION,
        owner_module=OWNER_MODULE,
        capability_id=INTEGRATOR_RECEIVE_CAPABILITY,
        capability_summary=CAPABILITY_SUMMARY,
        contract_version=CONTRACT_VERSION,
        destination_binding_id=binding.id,
        delivery_path=delivery_path,
        mirror_path=mirror_path,
        destination_scope=DESTINATION_SCOPE,
        activation_state=activation_state.value,
        source_revision=source_revision,
        descriptor_digest=descriptor_digest(published),
    )
