"""Remaining legacy database-backed ONT actions.

Customer-service configuration is owned by
``network.ont_service_configuration``. The former synchronous
``update_ont_config`` writer was removed at that cutover.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services import network as network_service
from app.services.network.ont_actions import ActionResult
from app.services.web_network_ont_actions._common import _log_action_audit


def set_voip_enabled(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    request: Request | None = None,
) -> ActionResult:
    """Set the legacy VoIP-enabled flag on an ONT."""

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    ont.voip_enabled = enabled
    db.commit()
    status = "enabled" if enabled else "disabled"
    _log_action_audit(
        db,
        request=request,
        action="set_voip_enabled",
        ont_id=ont_id,
        metadata={"voip_enabled": enabled},
    )
    return ActionResult(success=True, message=f"VoIP {status} on {ont.serial_number}")
