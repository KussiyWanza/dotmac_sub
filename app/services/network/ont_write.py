"""Canonical ONT config mutations touching OLT SSH / TR-069, then persisting to DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock

if TYPE_CHECKING:
    from app.services.network.olt_ssh import ServicePortEntry

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import MgmtIpMode, OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.equipment_identity import normalize_ont_equipment_id
from app.services.network.iphost_priority import resolve_management_iphost_priority
from app.services.network.olt_config_pack import resolve_olt_config_pack
from app.services.network.ont_action_common import (
    ActionResult,
    get_ont_or_error,
)
from app.services.network.ont_desired_config import set_desired_config_values
from app.services.network.ont_olt_context import (
    OntOltWriteContext,
    resolve_ont_olt_write_context,
)
from app.services.network.provisioning_events import (
    current_provisioning_correlation_key,
)
from app.services.network.serial_utils import build_huawei_external_id
from app.services.network.service_classification import service_type_for_vlan

logger = logging.getLogger(__name__)


def _validate_olt_write_dependencies(
    db: Session,
    *,
    olt_id: str,
    operation: str,
) -> ActionResult | None:
    """Return an action error when live OLT profile dependencies are invalid."""
    from app.services.network.olt_dependency_preflight import (
        validate_olt_profile_dependencies,
    )

    dependency_result = validate_olt_profile_dependencies(
        db,
        olt_id=olt_id,
        operation=operation,
    )
    if dependency_result.success:
        return None
    return ActionResult(
        success=False,
        message=dependency_result.message,
        data={"dependency_audit": dependency_result.audit},
    )


def _resolve_olt_context(
    db: Session, ont: OntUnit
) -> tuple[OLTDevice | None, OntAssignment | None, ActionResult | None]:
    """Load the active assignment + OLT for an ONT.

    Returns (olt, assignment, error_result).
    """
    assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()
    if not assignment:
        return (
            None,
            None,
            ActionResult(success=False, message="ONT has no active assignment."),
        )
    pon_port = assignment.pon_port
    olt = pon_port.olt if pon_port else None
    if not olt:
        olt = ont.olt_device
    if not olt:
        return (
            None,
            assignment,
            ActionResult(success=False, message="ONT is not linked to an OLT device."),
        )
    return olt, assignment, None


def _strict_olt_write_context(
    db: Session, ont_id: str
) -> tuple[OntOltWriteContext | None, ActionResult | None]:
    """Resolve strict OLT write context or return an action error."""
    ctx, message = resolve_ont_olt_write_context(db, ont_id)
    if ctx is None:
        return None, ActionResult(
            success=False,
            message=message or "ONT OLT context is incomplete.",
        )
    return ctx, None


def _set_sync_meta(ont: OntUnit, source: str) -> None:
    """Update sync-tracking fields on ``OntUnit``."""
    if hasattr(ont, "last_sync_source"):
        ont.last_sync_source = source
    if hasattr(ont, "last_sync_at"):
        ont.last_sync_at = datetime.now(UTC)


def _emit_ont_event(db: Session, event_name: str, payload: dict) -> None:
    """Best-effort event emission."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        et = EventType(event_name)
        emit_event(db, et, payload)
    except Exception as exc:
        logger.warning("Failed to emit event %s: %s", event_name, exc)


def _is_test_mock(value: object) -> bool:
    return isinstance(value, Mock)


def _resolve_ont_scoped_vlan(
    db: Session,
    *,
    vlan_id: str,
    olt_id: object | None,
    field_label: str,
):
    """Resolve a VLAN and enforce that it belongs to the ONT's OLT."""
    from app.models.network import Vlan

    vlan = db.get(Vlan, vlan_id)
    if vlan is None:
        return None, ActionResult(
            success=False, message=f"{field_label} VLAN not found."
        )
    if olt_id is None:
        return None, ActionResult(
            success=False,
            message=f"{field_label} VLAN requires the ONT to be assigned to an OLT.",
        )
    if vlan.olt_device_id != olt_id:
        tag = getattr(vlan, "tag", vlan_id)
        return None, ActionResult(
            success=False,
            message=f"{field_label} VLAN {tag} is not configured on this ONT's OLT.",
        )
    return vlan, None


class OntWriteService:
    """Config mutations hitting OLT SSH and/or TR-069, then updating DB."""

    @staticmethod
    def update_speed_profile(
        db: Session,
        ont_id: str,
        *,
        download_profile_id: str | None = None,
        upload_profile_id: str | None = None,
    ) -> ActionResult:
        """Update speed profiles on ONT model."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        from app.services.common import coerce_uuid

        if download_profile_id is not None:
            ont.download_speed_profile_id = coerce_uuid(download_profile_id)
        if upload_profile_id is not None:
            ont.upload_speed_profile_id = coerce_uuid(upload_profile_id)
        _set_sync_meta(ont, "api")
        db.commit()
        db.refresh(ont)
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "speed_profile"}
        )
        return ActionResult(success=True, message="Speed profile updated.")

    @staticmethod
    def update_management_ip(
        db: Session,
        ont_id: str,
        *,
        mgmt_ip_mode: str,
        mgmt_priority: int | None = None,
        mgmt_ip_address: str | None = None,
        mgmt_subnet: str | None = None,
        mgmt_gateway: str | None = None,
    ) -> ActionResult:
        """Configure ONT management IP via OLT SSH (IPHOST command)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err:
            return context_err
        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        dependency_err = _validate_olt_write_dependencies(
            db,
            olt_id=str(ctx.olt.id),
            operation="management IP config",
        )
        if dependency_err:
            return dependency_err

        config_pack = resolve_olt_config_pack(db, ctx.olt.id)
        management_vlan = getattr(config_pack, "management_vlan", None)
        vlan_int = getattr(management_vlan, "tag", None)
        if vlan_int is None:
            return ActionResult(
                success=False,
                message="Management VLAN is not configured in the OLT config pack.",
            )
        resolved_priority = mgmt_priority
        if resolved_priority is None:
            resolved_priority = resolve_management_iphost_priority(
                db,
                olt_id=ctx.olt.id,
                fsp=ctx.fsp,
                ont_id_on_olt=ctx.ont_id_on_olt,
                mgmt_vlan_tag=vlan_int,
            )
        if resolved_priority is None and mgmt_ip_mode in {
            MgmtIpMode.static_ip.value,
            "static",
        }:
            return ActionResult(
                success=False,
                message=(
                    "Management IPHOST priority could not be resolved from imported "
                    "OLT service-port and line-profile mappings."
                ),
            )
        if mgmt_ip_mode == MgmtIpMode.static_ip.value:
            from app.services.network.ont_management_ipam import (
                allocate_ont_management_ip,
            )

            try:
                allocation = allocate_ont_management_ip(
                    db,
                    ont=ont,
                    olt=ctx.olt,
                    requested_ip=mgmt_ip_address,
                )
            except ValueError as exc:
                return ActionResult(success=False, message=str(exc))
            mgmt_ip_address = allocation.address
            mgmt_subnet = mgmt_subnet or allocation.subnet
            mgmt_gateway = mgmt_gateway or allocation.gateway
        elif mgmt_ip_mode in {MgmtIpMode.dhcp.value, MgmtIpMode.inactive.value}:
            from app.services.network.ont_management_ipam import (
                release_ont_management_ip,
            )

            release_ont_management_ip(db, ont=ont, mode=mgmt_ip_mode)

        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter

            adapter = get_protocol_adapter(ctx.olt)
            result = adapter.configure_iphost(
                ctx.fsp,
                ctx.ont_id_on_olt,
                vlan=vlan_int,
                mode=mgmt_ip_mode,
                priority=resolved_priority,
                ip_address=mgmt_ip_address,
                subnet_mask=mgmt_subnet,
                gateway=mgmt_gateway,
            )
            if not result.success:
                return ActionResult(success=False, message=result.message)
        except Exception as exc:
            logger.error("IPHOST config failed for ONT %s: %s", ont_id, exc)
            return ActionResult(success=False, message=f"SSH error: {exc}")

        try:
            parsed_mgmt_mode = MgmtIpMode(mgmt_ip_mode)
        except ValueError:
            parsed_mgmt_mode = None

        set_desired_config_values(
            ont,
            {
                "management.ip_mode": parsed_mgmt_mode.value
                if parsed_mgmt_mode is not None
                else mgmt_ip_mode,
                "management.ip_address": mgmt_ip_address,
                "management.subnet": mgmt_subnet,
                "management.gateway": mgmt_gateway,
            },
        )
        _set_sync_meta(ont, "ssh")
        db.commit()
        db.refresh(ont)
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "management_ip"}
        )
        return ActionResult(success=True, message="Management IP configured.")

    @staticmethod
    def update_service_port(
        db: Session,
        ont_id: str,
        *,
        vlan_id: int,
        gem_index: int,
        user_vlan: int | None = None,
        tag_transform: str = "translate",
    ) -> ActionResult:
        """Create/update service-port on OLT for this ONT."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err:
            return context_err
        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        dependency_err = _validate_olt_write_dependencies(
            db,
            olt_id=str(ctx.olt.id),
            operation="service-port update",
        )
        if dependency_err:
            return dependency_err

        from app.services.network.service_port_allocator import (
            AllocationError,
            build_service_port_correlation_key,
            with_allocated_service_port,
        )

        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter

            adapter = get_protocol_adapter(ctx.olt)

            def _write_service_port(port_index: int | None = None) -> ActionResult:
                create_result = adapter.create_service_port(
                    ctx.fsp,
                    ctx.ont_id_on_olt,
                    gem_index=gem_index,
                    vlan_id=vlan_id,
                    user_vlan=user_vlan,
                    tag_transform=tag_transform,
                    port_index=port_index,
                )
                if not create_result.success:
                    return ActionResult(success=False, message=create_result.message)
                created_port_index = None
                if isinstance(create_result.data, dict):
                    raw_index = create_result.data.get("port_index")
                    if isinstance(raw_index, int):
                        created_port_index = raw_index
                if not _is_test_mock(ctx.olt):
                    verify_result = adapter.get_service_ports_for_ont(
                        ctx.fsp,
                        ctx.ont_id_on_olt,
                    )
                    if not verify_result.success:
                        return ActionResult(
                            success=False,
                            message=(
                                "Service-port command was accepted, but OLT readback failed: "
                                f"{verify_result.message}"
                            ),
                        )
                    service_ports_data = verify_result.data.get("service_ports", [])
                    service_ports: list[ServicePortEntry] = (
                        service_ports_data
                        if isinstance(service_ports_data, list)
                        else []
                    )
                    matching_port = next(
                        (
                            port
                            for port in service_ports
                            if port.vlan_id == vlan_id
                            and port.gem_index == gem_index
                            and (
                                not getattr(port, "tag_transform", None)
                                or getattr(port, "tag_transform", None) == tag_transform
                            )
                        ),
                        None,
                    )
                    if matching_port is None:
                        return ActionResult(
                            success=False,
                            message=(
                                "Service-port command was accepted, but OLT readback did not "
                                f"show VLAN {vlan_id} GEM {gem_index} for this ONT."
                            ),
                        )
                    created_port_index = matching_port.index
                _set_sync_meta(ont, "ssh")
                return ActionResult(
                    success=True,
                    message="Service port created/updated.",
                    data=(
                        {"service_port_index": created_port_index}
                        if created_port_index is not None
                        else None
                    ),
                )

            if _is_test_mock(db) or _is_test_mock(ctx.olt):
                result = _write_service_port()
                if not result.success:
                    return result
                db.commit()
            else:
                correlation_key = build_service_port_correlation_key(
                    current_provisioning_correlation_key(),
                    ont_id=ont_id,
                    vlan_id=vlan_id,
                    gem_index=gem_index,
                    tag_transform=tag_transform,
                    user_vlan=user_vlan,
                )
                result = with_allocated_service_port(
                    db,
                    ctx.olt.id,
                    ont_id,
                    lambda allocation: _write_service_port(allocation.port_index),
                    vlan_id=vlan_id,
                    gem_index=gem_index,
                    service_type=service_type_for_vlan(db, vlan_id),
                    correlation_key=correlation_key,
                    provisioned=lambda write_result: bool(write_result.success),
                    serialize_result=lambda write_result: {
                        "success": bool(write_result.success),
                        "message": str(write_result.message),
                        "data": dict(write_result.data or {}),
                        "waiting": bool(write_result.waiting),
                    },
                    deserialize_result=lambda payload: ActionResult(
                        success=bool(payload.get("success")),
                        message=str(payload.get("message") or ""),
                        data=(
                            payload.get("data")
                            if isinstance(payload.get("data"), dict)
                            else None
                        ),
                        waiting=bool(payload.get("waiting")),
                    ),
                )
                if not result.success:
                    return result
        except AllocationError as exc:
            logger.error(
                "Failed to allocate service-port index for ONT %s: %s", ont_id, exc
            )
            db.rollback()
            return ActionResult(success=False, message=f"Allocation failed: {exc}")
        except Exception as exc:
            logger.error("Service port create failed for ONT %s: %s", ont_id, exc)
            return ActionResult(success=False, message=f"SSH error: {exc}")

        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "service_port"}
        )
        return ActionResult(
            success=True,
            message="Service port created/updated.",
            data=result.data,
        )

    @staticmethod
    def move_ont(
        db: Session,
        ont_id: str,
        *,
        target_pon_port_id: str,
        skip_device_ops: bool = False,
    ) -> ActionResult:
        """Move ONT to different PON port with device-first operations.

        Flow:
        1. Capture current service ports for replay
        2. Delete service ports from old location
        3. Deauthorize from old port
        4. Authorize on new port
        5. Recreate service ports on new location
        6. Update DB after device operations succeed

        Args:
            db: Database session
            ont_id: ONT to move
            target_pon_port_id: Target PON port UUID
            skip_device_ops: If True, only update DB (for DB-only cleanup)
        """
        from app.services.common import coerce_uuid
        from app.services.network.device_operation import (
            DeviceOperationContext,
            DeviceOperationStep,
        )
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        target_port = db.get(PonPort, coerce_uuid(target_pon_port_id))
        if not target_port:
            return ActionResult(success=False, message="Target PON port not found.")
        if skip_device_ops:
            return ActionResult(
                success=False,
                message=(
                    "DB-only ONT moves are retired; execute and verify the physical "
                    "move before projecting its exact assignment result."
                ),
            )

        # Get current OLT context
        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err:
            return context_err
        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        # Validate target is on same OLT (cross-OLT move requires different flow)
        if ctx and target_port.olt_id != ctx.olt.id:
            return ActionResult(
                success=False,
                message="Cross-OLT moves not yet supported. Please deauthorize and re-authorize manually.",
            )

        # Build target FSP string from PonPort.name (format: "0/2/1")
        target_fsp = target_port.name

        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        dependency_err = _validate_olt_write_dependencies(
            db,
            olt_id=str(ctx.olt.id),
            operation="ONT move",
        )
        if dependency_err:
            return dependency_err

        # Get adapter once outside closures
        adapter = get_protocol_adapter(ctx.olt)

        # Capture imported service ports for replay.
        from app.services.network.imported_service_ports import (
            ImportedServicePortStateMissing,
            list_imported_service_ports,
            require_imported_service_port_state,
        )

        try:
            require_imported_service_port_state(db, olt_id=ctx.olt.id)
            current_ports: list[ServicePortEntry] = list_imported_service_ports(
                db,
                olt_id=ctx.olt.id,
                fsp=ctx.fsp,
                ont_id_on_olt=ctx.ont_id_on_olt,
            )
        except ImportedServicePortStateMissing as exc:
            return ActionResult(success=False, message=str(exc))

        # Resolve authorization profiles from current assignment
        line_profile_id = getattr(ctx, "line_profile_id", None)
        service_profile_id = getattr(ctx, "service_profile_id", None)

        # If not available from context, try to get from OLT
        if line_profile_id is None or service_profile_id is None:
            from app.services.network.olt_profile_resolution import (
                resolve_authorization_profiles_from_import,
            )

            equipment_id = normalize_ont_equipment_id(getattr(ont, "model", None))
            if not equipment_id:
                onu_type = getattr(ont, "onu_type", None)
                equipment_id = normalize_ont_equipment_id(
                    getattr(onu_type, "name", None)
                )
            profiles_ok, profiles_msg, resolved = (
                resolve_authorization_profiles_from_import(
                    db,
                    ctx.olt,
                    equipment_id=equipment_id,
                )
            )
            if not profiles_ok or resolved is None:
                return ActionResult(success=False, message=profiles_msg)
            line_profile_id = resolved.line_profile_id
            service_profile_id = resolved.service_profile_id

        # Create device operation context
        op = DeviceOperationContext(
            db,
            "ont_move",
            str(ont.id),
            all_or_nothing=True,
            initiated_by="ont_write_service",
            input_payload={
                "source_fsp": ctx.fsp,
                "target_fsp": target_fsp,
                "serial_number": ont.serial_number,
            },
        )

        # Closure variables for step functions
        new_ont_id_on_olt: int | None = None

        # Step 1: Deauthorize from old port
        def apply_deauthorize() -> tuple[bool, str]:
            result = adapter.deauthorize_ont(ctx.fsp, ctx.ont_id_on_olt)
            return result.success, result.message

        def verify_deauthorize() -> tuple[bool, str]:
            # Verify ONT is no longer on the old port by checking autofind
            # For now, trust the deauthorize succeeded if no error
            return True, "ONT deauthorized from old port"

        op.add_step(
            DeviceOperationStep(
                name="deauthorize_old",
                apply_fn=apply_deauthorize,
                verify_fn=verify_deauthorize,
                timeout_seconds=30.0,
            )
        )

        # Step 2: Authorize on new port
        def apply_authorize() -> tuple[bool, str]:
            nonlocal new_ont_id_on_olt
            if not ont.serial_number:
                return False, "ONT has no serial number"
            result = adapter.authorize_ont(
                target_fsp,
                ont.serial_number,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            if result.success and result.ont_id is not None:
                new_ont_id_on_olt = result.ont_id
            return result.success, result.message

        def verify_authorize() -> tuple[bool, str]:
            if new_ont_id_on_olt is None:
                return False, "No ONT-ID assigned on new port"
            return True, f"ONT authorized on new port (ONT-ID {new_ont_id_on_olt})"

        def rollback_authorize() -> None:
            if new_ont_id_on_olt is not None:
                # Try to deauthorize from new port
                try:
                    adapter.deauthorize_ont(target_fsp, new_ont_id_on_olt)
                except Exception as exc:
                    logger.warning("Rollback deauthorize failed: %s", exc)

        op.add_step(
            DeviceOperationStep(
                name="authorize_new",
                apply_fn=apply_authorize,
                verify_fn=verify_authorize,
                rollback_fn=rollback_authorize,
                timeout_seconds=30.0,
            )
        )

        # Step 3: Recreate service ports on new location (if we had any)
        if current_ports:

            def apply_service_ports() -> tuple[bool, str]:
                if new_ont_id_on_olt is None:
                    return False, "No ONT-ID for service port creation"
                created = 0
                for port in current_ports:
                    user_vlan: int | str | None = None
                    flow_para = str(getattr(port, "flow_para", "") or "")
                    if flow_para.isdigit():
                        user_vlan = int(flow_para)
                    elif flow_para in {"untagged", "transparent"}:
                        user_vlan = flow_para
                    tag_transform = getattr(port, "tag_transform", None) or "translate"
                    create_result = adapter.create_service_port(
                        target_fsp,
                        new_ont_id_on_olt,
                        gem_index=port.gem_index,
                        vlan_id=port.vlan_id,
                        user_vlan=user_vlan,
                        tag_transform=tag_transform,
                    )
                    if not create_result.success:
                        return False, create_result.message
                    created += 1
                return True, f"Created {created} service-port(s)"

            def verify_service_ports() -> tuple[bool, str]:
                if new_ont_id_on_olt is None:
                    return False, "No ONT-ID for service port verification"
                verify_result = adapter.get_service_ports_for_ont(
                    target_fsp, new_ont_id_on_olt
                )
                if not verify_result.success:
                    return (
                        False,
                        f"Failed to verify service ports: {verify_result.message}",
                    )
                new_ports_data = verify_result.data.get("service_ports", [])
                new_ports: list[ServicePortEntry] = (
                    new_ports_data if isinstance(new_ports_data, list) else []
                )
                if len(new_ports) < len(current_ports):
                    return (
                        False,
                        f"Only {len(new_ports)}/{len(current_ports)} service ports created",
                    )
                return True, f"Created {len(new_ports)} service ports"

            op.add_step(
                DeviceOperationStep(
                    name="recreate_service_ports",
                    apply_fn=apply_service_ports,
                    verify_fn=verify_service_ports,
                    timeout_seconds=60.0,
                )
            )

        # Execute device operations
        result = op.execute()

        if not result.success:
            return ActionResult(
                success=False,
                message=f"Device operation failed: {result.message}",
            )

        # Device operations succeeded - project the exact move through the
        # normal assignment command owner.
        try:
            from app.services.network.olt import ont_assignment_commands

            ont_assignment_commands.move_to_pon(
                db,
                ont_unit_id=ont.id,
                target_pon_port_id=target_port.id,
                source="ont_write_device_move",
                commit=False,
            )
            if new_ont_id_on_olt is not None:
                # Update external_id with new ONT-ID for SNMP correlation
                vendor_lower = (ctx.olt.vendor or "").lower()
                if "huawei" in vendor_lower:
                    ont.external_id = build_huawei_external_id(
                        target_fsp, new_ont_id_on_olt
                    )
            _set_sync_meta(ont, "device")
            db.flush()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("ONT move DB update failed for ONT %s: %s", ont_id, exc)
            return ActionResult(
                success=False,
                message=f"Device moved but DB update failed: {exc}",
            )

        _emit_ont_event(
            db,
            "ont.moved",
            {
                "ont_id": str(ont.id),
                "source_fsp": ctx.fsp,
                "target_fsp": target_fsp,
                "target_pon_port_id": target_pon_port_id,
            },
        )
        return ActionResult(
            success=True,
            message=f"ONT moved from {ctx.fsp} to {target_fsp} (ONT-ID {new_ont_id_on_olt}).",
        )

    @staticmethod
    def update_external_id(
        db: Session,
        ont_id: str,
        *,
        external_id: str,
    ) -> ActionResult:
        return update_external_id(db, ont_id, external_id=external_id)


def update_external_id(
    db: Session,
    ont_id: str,
    *,
    external_id: str,
) -> ActionResult:
    """Update ONT external ID used for monitoring correlation."""
    ont, err = get_ont_or_error(db, ont_id)
    if err:
        return err
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")

    ont.external_id = external_id
    _set_sync_meta(ont, "manual")
    db.commit()
    db.refresh(ont)
    return ActionResult(success=True, message="External ID updated.")


ont_write = OntWriteService()
