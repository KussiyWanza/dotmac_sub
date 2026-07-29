"""Administrative control of physical OLT PON ports."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, PonPort
from app.services.network.olt_ssh_ont._common import (
    _run_ont_config_command,
    normalize_fsp,
)
from app.services.network.olt_vendor_adapters import get_olt_adapter
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.pon_crud import PonPorts
from app.services.network.provisioning_settings import get_olt_write_mode_enabled


def set_pon_port_admin_state(
    db: Session,
    *,
    olt_id: str,
    pon_port_id: str,
    enabled: bool,
    request: Request | None = None,
) -> tuple[bool, str]:
    """Enable or disable one physical Huawei GPON port over SSH."""
    try:
        olt_uuid = UUID(str(olt_id))
        port_uuid = UUID(str(pon_port_id))
    except ValueError:
        return False, "Invalid OLT or PON port ID"

    olt = db.get(OLTDevice, olt_uuid)
    port = db.get(PonPort, port_uuid)
    if olt is None:
        return False, "OLT not found"
    if port is None or port.olt_id != olt.id:
        return False, "PON port not found on this OLT"

    target = "enabled" if enabled else "disabled"
    if port.admin_enabled is enabled:
        return True, f"PON port {port.name} is already {target}"

    if not get_olt_write_mode_enabled(db):
        return False, "OLT write mode is disabled; the PON port was not changed"

    try:
        adapter = get_olt_adapter(olt)
    except ValueError as exc:
        return False, str(exc)
    if adapter.vendor_name.lower() != "huawei" or not adapter.supports_ssh():
        return (
            False,
            f"Direct PON port control is not supported for {adapter.vendor_name} OLTs",
        )

    fsp = normalize_fsp(port.name)
    port_number = fsp.rsplit("/", 1)[-1]
    command = f"undo shutdown {port_number}" if enabled else f"shutdown {port_number}"
    ok, message = _run_ont_config_command(
        olt,
        fsp,
        command,
        success_message=f"PON port {port.name} {target} on {olt.name}",
    )

    log_olt_audit_event(
        db,
        request=request,
        action="set_pon_port_admin_state",
        entity_type="pon_port",
        entity_id=port.id,
        metadata={
            "olt_id": str(olt.id),
            "olt_name": olt.name,
            "port": port.name,
            "enabled": enabled,
            "transport": "ssh",
            "vendor": adapter.vendor_name,
            "message": message,
        },
        status_code=200 if ok else 502,
        is_success=ok,
    )

    if not ok:
        return False, message

    PonPorts.set_admin_enabled(db, port, enabled=enabled)
    return True, message
