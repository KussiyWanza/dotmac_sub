"""DB-free runtime contract for signed fiber website inquiry ingress."""

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

FIBER_INQUIRY_CAPABILITY = "communications.fiber_inquiry.receive.v1"


class FiberInquiryHttpRunner:
    """Inbound-only connector; the HTTP adapter verifies and normalizes input."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, object],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        missing = tuple(
            key
            for key in (
                "signature_header",
                "delivery_id_header",
                "signature_prefix",
                "site_id",
            )
            if not str(config.get(key) or "").strip()
        )
        if not secret_material.get("webhook_signing_secret"):
            missing += ("webhook_signing_secret",)
        return ValidationResult(
            valid=not missing,
            error_codes=tuple(f"{key}_missing" for key in missing),
        )

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
        result = self.validate(
            manifest=manifest,
            config=config,
            secret_material=secret_material,
        )
        return HealthResult(
            status="healthy" if result.valid else "misconfigured",
            details={"error_codes": list(result.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False
