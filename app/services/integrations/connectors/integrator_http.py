"""DB-free runtime contract for the independently deployed Integrator.

The Integrator is a transport, not a connector Sub executes. It runs in its own
deployment, talks to the provider itself, and pushes provider-neutral
observations into Sub's port. Sub therefore never *calls* this runner: every
operation is rejected, exactly as ``lead_capture_http`` rejects its own.

It exists as a connector definition for one reason, and it is a good one. The
transport receipt ledger (``integration_inbox``) is keyed on a capability
binding, so the Integrator needs a binding of its own. Giving it one — rather
than letting it borrow the WhatsApp connector's binding — is what makes the
transport separable: its receipts, its quarantine state, its enable/disable
switch and its eventual retirement are all its own, and retiring Sub's direct
WhatsApp installation later does not disturb it.

It declares no secret binding. The Integrator authenticates to Sub with a
scoped ``ApiKey``, which is a credential Sub issues and can revoke, not a shared
webhook secret this connector would have to hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

INTEGRATOR_CONNECTOR_KEY = "dotmac.integrator.http"
INTEGRATOR_RECEIVE_CAPABILITY = "messaging.receive.v1"


class IntegratorHttpRunner:
    """Inbound-only transport; the HTTP port performs verified receipt intake."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, object],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        # There is nothing to validate against a provider: the Integrator holds
        # the provider credentials in its own deployment. A binding is valid as
        # soon as it exists and is enabled.
        return ValidationResult(valid=True, error_codes=())

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, object],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.rejected,
            error_code="inbound_only_capability",
        )

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, object],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        return HealthResult(status="healthy", details={"error_codes": []})

    def cancel(self, operation_id: UUID) -> bool:
        return False
