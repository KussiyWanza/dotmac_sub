"""Application-facing adapter for OLT actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.services.adapters import AdapterResult, adapter_registry

if TYPE_CHECKING:
    from app.models.network import OLTDevice, OltFirmwareImage


class OltActionAdapter:
    name = "olt_action"

    def fetch_running_config(
        self, olt: OLTDevice, db: Session | None = None
    ) -> str | None:
        from app.services.network import olt_operations

        return olt_operations.fetch_running_config(olt, db=db)

    def get_ont_status_by_serial(
        self,
        db: Session,
        olt_id: str,
        serial_number: str,
        **kwargs: Any,
    ) -> AdapterResult:
        from app.services.network import olt_operations

        success, message, payload = olt_operations.get_ont_status_by_serial(
            db, olt_id, serial_number, **kwargs
        )
        if success:
            return AdapterResult.ok(message, data=payload)
        return AdapterResult.fail(message, data=payload)

    def get_olt_firmware_images(
        self, db: Session, olt_id: str
    ) -> list[OltFirmwareImage]:
        from app.services.network import olt_operations

        return olt_operations.get_olt_firmware_images(db, olt_id)


olt_action_adapter = OltActionAdapter()
adapter_registry.register(olt_action_adapter)
