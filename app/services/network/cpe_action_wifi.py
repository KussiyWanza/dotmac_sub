"""WiFi and LAN-related CPE device actions.

WiFi SSID/password changes for the CPE-detail admin page do NOT write to
GenieACS directly. The web adapter resolves an exact admission scope and this
module delegates a sparse patch to ``configure_cpe_wifi`` -- the
``network.ont_service_configuration`` owner. The owner locks and revalidates
the full CPE/TR-069/ONT/assignment chain before staging a durable revision, so
concurrent changes compose and an identity relink cannot redirect a stale
request. See ``app/web/admin/network_cpes.py`` for the route permissions and
object-scope authorization.

LAN port toggling is unaffected by this change and still writes to GenieACS
directly via ``set_and_verify``.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.services.domain_errors import DomainError
from app.services.genieacs_client import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_cpe_client_or_error,
    set_and_verify,
)
from app.services.network.ont_service_configuration import (
    ConfigureCpeWifiCommand,
    CpeWifiAdmissionScope,
    CpeWifiConfigurationChange,
    configure_cpe_wifi,
)
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

_LAN_PORT_PATHS = {
    "Device": "Ethernet.Interface.{port}.Enable",
    "InternetGatewayDevice": "LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable",
}


def _parse_cpe_uuid(cpe_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(cpe_id))
    except (TypeError, ValueError, AttributeError):
        return None


def set_wifi_ssid(
    db: Session,
    cpe_id: str,
    ssid: str,
    *,
    admission_scope: CpeWifiAdmissionScope,
    context: CommandContext,
    permission_granted: bool,
    object_scope_granted: bool,
) -> ActionResult:
    """Queue a WiFi SSID change on the ONT behind this CPE via its owner."""
    if not ssid or len(ssid) > 32:
        return ActionResult(success=False, message="SSID must be 1-32 characters.")

    cpe_uuid = _parse_cpe_uuid(cpe_id)
    if cpe_uuid is None:
        return ActionResult(
            success=False,
            message="Invalid CPE identifier.",
            error_code="network.ont_service_configuration.cpe_device_not_found",
        )
    if admission_scope.cpe_device_id != cpe_uuid:
        return ActionResult(
            success=False,
            message="The CPE admission identity changed; refresh and retry.",
            error_code="network.ont_service_configuration.cpe_identity_changed",
        )
    finish_read_transaction(db)

    try:
        outcome = configure_cpe_wifi(
            db,
            ConfigureCpeWifiCommand(
                context=context,
                cpe_device_id=cpe_uuid,
                expected_tr069_device_id=admission_scope.tr069_device_id,
                expected_ont_unit_id=admission_scope.ont_unit_id,
                expected_assignment_id=admission_scope.assignment_id,
                permission_granted=permission_granted,
                object_scope_granted=object_scope_granted,
                change=CpeWifiConfigurationChange(ssid=ssid),
            ),
        )
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    logger.info(
        "WiFi SSID change queued for CPE %s via ONT %s (operation %s)",
        cpe_id,
        admission_scope.ont_unit_id,
        outcome.operation_id,
    )
    return ActionResult(
        success=True,
        message=f"WiFi SSID update queued: {outcome.message}",
        data={
            "operation_id": str(outcome.operation_id),
            "ont_unit_id": str(admission_scope.ont_unit_id),
        },
    )


def set_wifi_password(
    db: Session,
    cpe_id: str,
    password: str,
    *,
    admission_scope: CpeWifiAdmissionScope,
    context: CommandContext,
    permission_granted: bool,
    object_scope_granted: bool,
) -> ActionResult:
    """Queue a WiFi password change on the ONT behind this CPE via its owner."""
    if not 8 <= len(password) <= 63:
        return ActionResult(
            success=False, message="WiFi password must be 8-63 characters."
        )

    cpe_uuid = _parse_cpe_uuid(cpe_id)
    if cpe_uuid is None:
        return ActionResult(
            success=False,
            message="Invalid CPE identifier.",
            error_code="network.ont_service_configuration.cpe_device_not_found",
        )
    if admission_scope.cpe_device_id != cpe_uuid:
        return ActionResult(
            success=False,
            message="The CPE admission identity changed; refresh and retry.",
            error_code="network.ont_service_configuration.cpe_identity_changed",
        )
    finish_read_transaction(db)

    try:
        outcome = configure_cpe_wifi(
            db,
            ConfigureCpeWifiCommand(
                context=context,
                cpe_device_id=cpe_uuid,
                expected_tr069_device_id=admission_scope.tr069_device_id,
                expected_ont_unit_id=admission_scope.ont_unit_id,
                expected_assignment_id=admission_scope.assignment_id,
                permission_granted=permission_granted,
                object_scope_granted=object_scope_granted,
                change=CpeWifiConfigurationChange(password=password),
            ),
        )
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    logger.info(
        "WiFi password change queued for CPE %s via ONT %s (operation %s)",
        cpe_id,
        admission_scope.ont_unit_id,
        outcome.operation_id,
    )
    return ActionResult(
        success=True,
        message=f"WiFi password update queued: {outcome.message}",
        data={
            "operation_id": str(outcome.operation_id),
            "ont_unit_id": str(admission_scope.ont_unit_id),
        },
    )


def toggle_lan_port(db: Session, cpe_id: str, port: int, enabled: bool) -> ActionResult:
    """Enable or disable a CPE LAN port via TR-069."""
    if port < 1 or port > 4:
        return ActionResult(
            success=False, message="Port number must be between 1 and 4."
        )

    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    value = "true" if enabled else "false"
    path = _LAN_PORT_PATHS[root].format(port=port)
    params = build_tr069_params(root, {path: value})
    try:
        result = set_and_verify(client, device_id, params)
        action_word = "enabled" if enabled else "disabled"
        logger.info("LAN port %d %s on CPE %s", port, action_word, cpe.serial_number)
        return ActionResult(
            success=True,
            message=f"LAN port {port} {action_word} on {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Toggle LAN port %d failed for CPE %s: %s", port, cpe.serial_number, exc
        )
        return ActionResult(success=False, message=f"Failed to toggle LAN port: {exc}")
