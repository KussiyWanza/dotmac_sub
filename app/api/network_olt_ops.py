"""OLT operational API — authorization, service-ports, profiles, CLI."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.network_olt_ops import (
    OltAuthorizationAdmissionRead,
    OltAuthorizationOperationResponse,
    OltAuthorizeOntRequest,
    OltCliCommandRequest,
    OltOntStatusBySerialRequest,
    OltOperationResponse,
    OltServicePortCreateRequest,
    OltTr069ProfileCreateRequest,
)
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError
from app.services.network import olt_api_operations
from app.services.network.action_logging import actor_label
from app.services.network.ont_authorization_contracts import (
    RequestAssignedOntAuthorization,
)
from app.services.owner_commands import CommandContext

router = APIRouter(tags=["network-olt-operations"])


# ── ONT Authorization ─────────────────────────────────────────────────


@router.post(
    "/olt-devices/{olt_id}/authorize-ont",
    response_model=OltAuthorizationOperationResponse,
    status_code=202,
    summary="Authorize and provision an assigned ONT",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def authorize_and_provision_ont(
    request: Request,
    olt_id: UUID,
    payload: OltAuthorizeOntRequest,
    db: Session = Depends(get_db),
) -> OltAuthorizationOperationResponse:
    try:
        result = olt_api_operations.authorize_and_provision_ont(
            db,
            RequestAssignedOntAuthorization.from_transport(
                context=CommandContext.system(
                    actor=actor_label(request),
                    scope="network:ont:authorize",
                    reason="API requested assigned ONT authorization",
                ),
                ont_id=payload.ont_id,
                olt_id=olt_id,
                fsp=payload.fsp,
                serial_number=payload.serial_number,
                force_reauthorize=payload.force_reauthorize,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltAuthorizationOperationResponse(
        success=result.success,
        message=result.message,
        data=OltAuthorizationAdmissionRead(
            operation_id=result.operation_id,
            dispatch_id=result.dispatch_id,
            waiting=result.waiting,
            duplicate=result.duplicate,
        ),
    )


@router.post(
    "/olt-devices/{olt_id}/ont-status-by-serial",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_ont_status_by_serial(
    request: Request,
    olt_id: str,
    payload: OltOntStatusBySerialRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    from app.services.network.olt_operations import get_ont_status_by_serial

    success, message, status = get_ont_status_by_serial(
        db, olt_id, payload.serial_number, request=request
    )
    if not success:
        raise HTTPException(status_code=422, detail=message)
    return OltOperationResponse(success=True, message=message, data=status)


# ── Service Ports ──────────────────────────────────────────────────────


@router.get(
    "/olt-devices/{olt_id}/service-ports",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def list_service_ports(
    olt_id: str,
    fsp: str = Query(description="Frame/Slot/Port e.g. 0/1/0"),
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.list_service_ports(db, olt_id, fsp=fsp)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=True,
        message=result.message,
        data=(result.data or {}).get("entries", []),
    )


@router.post(
    "/olt-devices/{olt_id}/service-ports",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def create_service_port(
    olt_id: str,
    payload: OltServicePortCreateRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.create_service_port(
        db,
        olt_id,
        fsp=payload.fsp,
        ont_id=payload.ont_id,
        gem_index=payload.gem_index,
        vlan_id=payload.vlan_id,
        user_vlan=payload.user_vlan,
        tag_transform=payload.tag_transform,
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.delete(
    "/olt-devices/{olt_id}/service-ports/{index}",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def delete_service_port(
    olt_id: str, index: int, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.delete_service_port(db, olt_id, index=index)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.get(
    "/olt-devices/{olt_id}/service-ports/ont/{ont_id}",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_service_ports_for_ont(
    olt_id: str,
    ont_id: int,
    fsp: str = Query(description="Frame/Slot/Port e.g. 0/1/0"),
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.list_service_ports_for_ont(
        db, olt_id, fsp=fsp, ont_id=ont_id
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=True,
        message=result.message,
        data=(result.data or {}).get("entries", []),
    )


# ── Profiles ───────────────────────────────────────────────────────────


@router.get(
    "/olt-devices/{olt_id}/profiles/line",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_line_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.get_line_profiles(db, olt_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=True,
        message=result.message,
        data=(result.data or {}).get("entries", []),
    )


@router.get(
    "/olt-devices/{olt_id}/profiles/service",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_service_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.get_service_profiles(db, olt_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=True,
        message=result.message,
        data=(result.data or {}).get("entries", []),
    )


@router.get(
    "/olt-devices/{olt_id}/profiles/tr069",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_tr069_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.get_tr069_profiles(db, olt_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=True,
        message=result.message,
        data=(result.data or {}).get("entries", []),
    )


@router.post(
    "/olt-devices/{olt_id}/profiles/tr069",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def create_tr069_profile(
    olt_id: str,
    payload: OltTr069ProfileCreateRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.create_tr069_profile(
        db,
        olt_id,
        profile_name=payload.name,
        acs_url=payload.acs_url,
        username=payload.username,
        password=payload.password,
        inform_interval=payload.inform_interval,
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


# ── Config & Connectivity ─────────────────────────────────────────────


@router.post(
    "/olt-devices/{olt_id}/config-backup",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def fetch_config_backup(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.run_config_backup(db, olt_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.post(
    "/olt-devices/{olt_id}/test-connection",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def test_olt_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    success, message, policy_key = olt_api_operations.test_ssh_connection(
        db, olt_id, request=request
    )
    if not success:
        raise HTTPException(status_code=422, detail=message)
    return OltOperationResponse(
        success=True,
        message=message,
        data={"version": policy_key, "policy_key": policy_key},
    )


@router.post(
    "/olt-devices/{olt_id}/cli-command",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def run_cli_command(
    request: Request,
    olt_id: str,
    payload: OltCliCommandRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.run_cli_command(
        db, olt_id, command=payload.command, request=request
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


# ── Config Pack Validation ─────────────────────────────────────────────


@router.get(
    "/olt-devices/{olt_id}/config-pack/validate",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def validate_config_pack(
    olt_id: str,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    """Validate OLT config pack for provisioning readiness.

    Returns validation status with warnings (non-blocking) and errors (blocking).
    Authorization can proceed with warnings but not errors.
    """
    from app.services.network.olt_config_pack import (
        get_validation_summary,
        validate_config_pack_comprehensive,
    )

    validation = validate_config_pack_comprehensive(db, olt_id)
    summary = get_validation_summary(validation)

    return OltOperationResponse(
        success=validation.is_valid,
        message=summary,
        data=validation.to_dict(),
    )


@router.get(
    "/olt-devices/{olt_id}/config-pack",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def get_config_pack(
    olt_id: str,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    """Get resolved OLT config pack for provisioning.

    Returns all default configuration values for ONT provisioning.
    """
    from app.services.network.olt_config_pack import resolve_olt_config_pack

    config_pack = resolve_olt_config_pack(db, olt_id)
    if config_pack is None:
        raise HTTPException(status_code=404, detail="OLT device not found")

    return OltOperationResponse(
        success=True,
        message="Config pack resolved successfully",
        data=config_pack.to_dict(),
    )
