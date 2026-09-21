"""Service helpers for remote CPE action web routes."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.auth_dependencies import has_permission
from app.services.domain_errors import DomainError
from app.services.network.cpe_actions import ActionResult, CpeActions
from app.services.network.ont_scope import can_manage_ont_from_request
from app.services.network.ont_service_configuration import (
    CpeWifiAdmissionScope,
    resolve_cpe_wifi_admission_scope,
)
from app.services.network_operations import run_tracked_action
from app.services.owner_commands import CommandContext
from app.services.web_network_cpe_audit import (
    actor_name_from_request,
    log_cpe_audit_event,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _ont_write_command_context(
    request, *, reason: str, idempotency_key: str
) -> CommandContext:
    """Build the ``network:ont:write``-scoped context a CPE-detail WiFi
    action needs to delegate into ``configure_ont_service`` -- the same
    context shape ``network_onts.py`` builds for the ONT Configure tab.
    """
    from app.services import web_admin as web_admin_service

    command_id = uuid.uuid4()
    request_id = str(getattr(request.state, "request_id", "") or "").strip()
    try:
        correlation_id = uuid.UUID(request_id)
    except ValueError:
        correlation_id = command_id
    auth = getattr(request.state, "auth", {}) or {}
    principal_type = str(auth.get("principal_type") or "user")
    actor_id = web_admin_service.get_actor_id(request) or "unknown"
    return CommandContext(
        command_id=command_id,
        correlation_id=correlation_id,
        actor=f"{principal_type}:{actor_id}",
        scope="network:ont:write",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _cpe_wifi_admission_from_request(
    db: Session, cpe_id: str, *, request
) -> tuple[CpeWifiAdmissionScope | None, ActionResult | None, bool]:
    """Resolve exact identity and evaluate ONT object scope before mutation."""
    try:
        cpe_uuid = uuid.UUID(str(cpe_id))
    except (TypeError, ValueError, AttributeError):
        return (
            None,
            ActionResult(
                success=False,
                message="Invalid CPE identifier.",
                error_code="network.ont_service_configuration.cpe_device_not_found",
            ),
            False,
        )
    try:
        scope = resolve_cpe_wifi_admission_scope(db, cpe_uuid)
    except DomainError as exc:
        return (
            None,
            ActionResult(success=False, message=exc.message, error_code=exc.code),
            False,
        )
    object_scope_granted = can_manage_ont_from_request(request, db, scope.ont_unit_id)
    return scope, None, object_scope_granted


def execute_reboot(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Reboot CPE device with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_reboot,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.reboot(db, cpe_id),
        correlation_key=f"cpe_reboot:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_reboot_from_request(db: Session, cpe_id: str, *, request) -> ActionResult:
    result = execute_reboot(db, cpe_id, initiated_by=actor_name_from_request(request))
    log_cpe_audit_event(
        db,
        request=request,
        action="reboot",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_factory_reset(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Factory reset CPE device with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_factory_reset,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.factory_reset(db, cpe_id),
        correlation_key=f"cpe_factory_reset:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_factory_reset_from_request(
    db: Session, cpe_id: str, *, request
) -> ActionResult:
    result = execute_factory_reset(
        db, cpe_id, initiated_by=actor_name_from_request(request)
    )
    log_cpe_audit_event(
        db,
        request=request,
        action="factory_reset",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_connection_request(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Send connection request to CPE with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_send_conn_request,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.send_connection_request(db, cpe_id),
        correlation_key=f"cpe_conn_req:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_connection_request_from_request(
    db: Session, cpe_id: str, *, request
) -> ActionResult:
    return execute_connection_request(
        db, cpe_id, initiated_by=actor_name_from_request(request)
    )


def execute_refresh_from_request(db: Session, cpe_id: str, *, request) -> ActionResult:
    result = CpeActions.refresh_status(db, cpe_id)
    log_cpe_audit_event(
        db,
        request=request,
        action="refresh",
        entity_id=cpe_id,
        metadata={"success": result.success},
        is_success=result.success,
    )
    return result


def execute_wifi_ssid_from_request(
    db: Session, cpe_id: str, *, ssid: str, request
) -> ActionResult:
    """Queue a WiFi SSID change via the ONT service-configuration owner.

    Requires ``network:ont:write`` (checked here, in addition to the
    ``network:cpe:write`` FastAPI dependency already gating this route)
    because the change is admitted and dispatched by
    ``network.ont_service_configuration``, which enforces that scope itself.
    """
    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    admission_scope, admission_error, object_scope_granted = (
        _cpe_wifi_admission_from_request(db, cpe_id, request=request)
    )
    if admission_error is not None:
        log_cpe_audit_event(
            db,
            request=request,
            action="set_wifi_ssid",
            entity_id=cpe_id,
            metadata={"success": False, "message": admission_error.message},
            is_success=False,
        )
        return admission_error
    assert admission_scope is not None
    context = _ont_write_command_context(
        request,
        reason="Set WiFi SSID from CPE detail",
        idempotency_key=f"cpe-wifi-ssid:{cpe_id}:{uuid.uuid4()}",
    )
    result = CpeActions.set_wifi_ssid(
        db,
        cpe_id,
        ssid,
        admission_scope=admission_scope,
        context=context,
        permission_granted=permission_granted,
        object_scope_granted=object_scope_granted,
    )
    log_cpe_audit_event(
        db,
        request=request,
        action="set_wifi_ssid",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_wifi_password_from_request(
    db: Session, cpe_id: str, *, password: str, request
) -> ActionResult:
    """Queue a WiFi password change via the ONT service-configuration owner.

    Requires ``network:ont:write`` for the same reason as
    ``execute_wifi_ssid_from_request`` above.
    """
    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    admission_scope, admission_error, object_scope_granted = (
        _cpe_wifi_admission_from_request(db, cpe_id, request=request)
    )
    if admission_error is not None:
        log_cpe_audit_event(
            db,
            request=request,
            action="set_wifi_password",
            entity_id=cpe_id,
            metadata={"success": False, "message": admission_error.message},
            is_success=False,
        )
        return admission_error
    assert admission_scope is not None
    context = _ont_write_command_context(
        request,
        reason="Set WiFi password from CPE detail",
        idempotency_key=f"cpe-wifi-password:{cpe_id}:{uuid.uuid4()}",
    )
    result = CpeActions.set_wifi_password(
        db,
        cpe_id,
        password,
        admission_scope=admission_scope,
        context=context,
        permission_granted=permission_granted,
        object_scope_granted=object_scope_granted,
    )
    log_cpe_audit_event(
        db,
        request=request,
        action="set_wifi_password",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_lan_port(
    db: Session, cpe_id: str, *, port: int, enabled: bool
) -> ActionResult:
    return CpeActions.toggle_lan_port(db, cpe_id, port, enabled)


def execute_ping_diagnostic(
    db: Session, cpe_id: str, *, host: str, count: int
) -> ActionResult:
    return CpeActions.run_ping_diagnostic(db, cpe_id, host, count)


def execute_traceroute_diagnostic(
    db: Session, cpe_id: str, *, host: str
) -> ActionResult:
    return CpeActions.run_traceroute_diagnostic(db, cpe_id, host)
