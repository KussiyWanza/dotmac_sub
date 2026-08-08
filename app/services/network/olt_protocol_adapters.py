"""OLT Protocol Adapter for SSH-based OLT operations.

Provides a clean interface for OLT write operations via SSH CLI.
NETCONF is used as an optimization for ONT authorization when available,
with automatic fallback to SSH.

Usage:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    adapter = get_protocol_adapter(olt)
    result = adapter.authorize_ont(fsp="0/1/0", serial="HWTC12345678", ...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from uuid import UUID

from app.services.adapters.base import AdapterResult
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiCliResource,
    classify_huawei_cli_response,
    is_huawei_resource_absent,
    project_response_code_evidence,
)
from app.services.network.olt_ssh_ont._common import OntAuthorizationOutcome
from app.services.network.parsers.cli import canonical_fsp

if TYPE_CHECKING:
    from app.models.network import OLTDevice
    from app.services.network.olt_batched_mgmt import BatchedMgmtSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OltConnectionConfig:
    """Detached connection values safe to use outside a database transaction."""

    id: UUID
    name: str
    hostname: str | None
    mgmt_ip: str | None
    vendor: str | None
    model: str | None
    firmware_version: str | None
    software_version: str | None
    ssh_username: str | None
    ssh_password: str | None
    ssh_port: int | None

    @classmethod
    def from_model(cls, olt: OLTDevice) -> OltConnectionConfig:
        return cls(
            id=olt.id,
            name=olt.name,
            hostname=olt.hostname,
            mgmt_ip=olt.mgmt_ip,
            vendor=olt.vendor,
            model=olt.model,
            firmware_version=olt.firmware_version,
            software_version=olt.software_version,
            ssh_username=olt.ssh_username,
            ssh_password=olt.ssh_password,
            ssh_port=olt.ssh_port,
        )


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class OltOperationResult(AdapterResult):
    """Result of an OLT write operation.

    ``response_code`` is the authoritative device verdict. Set it from the
    classification made on **raw** device output at the point of capture;
    callers branch on it. The ``message`` fallback below exists only for
    transports that have not yet been migrated to
    :class:`~app.services.network.huawei_cli_response.HuaweiDeviceOutcome`,
    and cannot be relied on: ``message`` is wrapped and truncated.
    """

    # For authorize_ont: the assigned ONT ID
    ont_id: int | None = None

    # If NETCONF fallback occurred, the reason
    fallback_reason: str | None = None

    # For create_service_port: the assigned service-port index
    service_port_index: int | None = None

    # Typed device verdict, classified once on raw output.
    response_code: HuaweiCliErrorCode | None = None

    def __post_init__(self) -> None:
        """Attach sanitized Huawei response evidence before callers project it."""
        response_code = self.response_code
        if response_code is None:
            # Legacy path: recover what we can from the operator-facing text.
            response_code = classify_huawei_cli_response(self.message).error_code
            self.response_code = response_code
        if response_code == HuaweiCliErrorCode.NONE:
            return
        self.data = dict(self.data or {})
        self.data.setdefault(
            "huawei_cli_response", project_response_code_evidence(response_code)
        )
        if self.error_code is None:
            self.error_code = response_code.value


@runtime_checkable
class OltProtocolAdapterContract(Protocol):
    """Contract consumed by ONT authorization/provisioning workflows.

    Implementations may use SSH, NETCONF, or another transport, but callers
    should depend on this operation surface rather than transport details.
    """

    @property
    def olt(self) -> OLTDevice: ...

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult: ...

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult: ...

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult: ...

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult: ...

    def set_ont_description(
        self,
        fsp: str,
        ont_id: int,
        description: str,
    ) -> OltOperationResult: ...

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult: ...

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult: ...

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult: ...

    def delete_service_port(self, port_index: int) -> OltOperationResult: ...

    def configure_management_batch(
        self,
        spec: BatchedMgmtSpec,
    ) -> OltOperationResult: ...

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult: ...

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult: ...

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult: ...

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult: ...

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        priority: int = 0,
        username: str,
        password: str,
    ) -> OltOperationResult: ...

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult: ...

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult: ...

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult: ...

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult: ...

    def unbind_tr069_profile(self, fsp: str, ont_id: int) -> OltOperationResult: ...

    def get_service_ports(self, fsp: str) -> OltOperationResult: ...

    def get_service_ports_for_ont(
        self, fsp: str, ont_id: int
    ) -> OltOperationResult: ...

    def get_line_profiles(self) -> OltOperationResult: ...

    def get_service_profiles(self) -> OltOperationResult: ...

    def get_tr069_profiles(self) -> OltOperationResult: ...

    def get_tr069_profile_binding(
        self, fsp: str, ont_id: int
    ) -> OltOperationResult: ...

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult: ...

    def diagnose_service_ports(self, fsp: str, ont_id: int) -> OltOperationResult: ...

    def fetch_running_config(self) -> OltOperationResult: ...


# ============================================================================
# Protocol Adapter
# ============================================================================


class OltProtocolAdapter:
    """SSH-based protocol adapter for OLT operations.

    Uses SSH CLI for all operations. For authorize_ont(), tries NETCONF first
    when enabled on the OLT, with automatic fallback to SSH.
    """

    def __init__(self, olt: OLTDevice):
        self._olt = olt

    @property
    def olt(self) -> OLTDevice:
        return self._olt

    def _not_supported(self, operation: str) -> OltOperationResult:
        """Return a 'not supported' result for an operation."""
        return OltOperationResult(
            success=False,
            message=f"{operation} not supported",
        )

    # ========== ONT Lifecycle ==========

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize ONT on the OLT via SSH."""
        return self._ssh_authorize(
            fsp,
            serial_number,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            description=description,
        )

    def _ssh_authorize(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize ONT via SSH CLI, confirming the registration by readback.

        Mirrors ``deauthorize_ont``: the device write is never trusted on its
        own. A shelf that returns no verdict, or accepts without naming an
        ONT-ID, is resolved by reading the registration back — the previous
        code reported "command sent" as a success with no ONT-ID, which the
        authorization workflow then had to treat as a failure.
        """
        from app.services.network.olt_ssh_ont import authorize_ont as ssh_authorize

        try:
            outcome = ssh_authorize(
                self._olt,
                fsp,
                serial_number,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
                description=description or None,
            )
        except Exception as exc:
            logger.exception("SSH authorize_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH authorization failed: {exc}",
                response_code=HuaweiCliErrorCode.CONNECTION_ERROR,
            )

        if outcome.succeeded and outcome.ont_id is not None:
            return OltOperationResult(
                success=True,
                message=outcome.message,
                ont_id=outcome.ont_id,
                response_code=outcome.code,
            )

        # Accepted without an ONT-ID (MA5800 reports a success count only), or
        # no verdict at all: ask the device what it actually holds.
        if outcome.succeeded or outcome.device_was_silent:
            return self._confirm_authorization_by_readback(
                fsp,
                serial_number,
                outcome=outcome,
            )

        # An explicit rejection is final. Carry the typed code so the
        # authorization workflow can branch on it (for example
        # SERIAL_ALREADY_EXISTS -> reuse or move the existing registration)
        # without re-parsing the operator-facing message.
        return OltOperationResult(
            success=False,
            message=outcome.message,
            response_code=outcome.code,
        )

    def _confirm_authorization_by_readback(
        self,
        fsp: str,
        serial_number: str,
        *,
        outcome: OntAuthorizationOutcome,
    ) -> OltOperationResult:
        """Resolve an unconfirmed authorization against the device's own state."""
        read = self.find_ont_by_serial(serial_number)
        registration = read.data.get("registration") if read.success else None
        if not read.success or registration is None:
            detail = read.message if not read.success else "ONT is not registered"
            return OltOperationResult(
                success=False,
                message=f"{outcome.message} Readback did not confirm it: {detail}",
                response_code=outcome.code,
                data={"verified_registered": False},
            )

        observed_fsp = str(getattr(registration, "fsp", "") or "").strip()
        requested = canonical_fsp(fsp)
        if requested is None or observed_fsp != requested.fsp:
            return OltOperationResult(
                success=False,
                message=(
                    f"{outcome.message} Readback found the serial on "
                    f"{observed_fsp or 'an unknown port'}, not {fsp}."
                ),
                response_code=outcome.code,
                data={"verified_registered": False},
            )

        ont_id = int(registration.onu_id)
        return OltOperationResult(
            success=True,
            message=(
                f"ONT {serial_number} confirmed registered on {requested.fsp} "
                f"as ONT-ID {ont_id} by readback."
            ),
            ont_id=ont_id,
            response_code=outcome.code,
            data={"verified_registered": True},
        )

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Deauthorize an ONT and verify that it is absent on readback."""
        from app.services.network.olt_ssh_ont import (
            deauthorize_ont,
            get_ont_status,
        )

        try:
            ok, message = deauthorize_ont(self._olt, fsp, ont_id)
            if not ok:
                return OltOperationResult(success=False, message=message)

            read_ok, read_message, status = get_ont_status(self._olt, fsp, ont_id)
            if read_ok and status is not None:
                return OltOperationResult(
                    success=False,
                    message=(
                        f"{message}; verification failed: ONT {ont_id} still exists "
                        f"on {fsp}"
                    ),
                    data={"verified_absent": False},
                )
            absent = is_huawei_resource_absent(
                read_message,
                HuaweiCliResource.ONT,
            )
            if not absent:
                return OltOperationResult(
                    success=False,
                    message=(
                        f"{message}; deauthorization was accepted but absence "
                        f"readback failed: {read_message}"
                    ),
                    data={"verified_absent": False},
                )
            return OltOperationResult(
                success=True,
                message=f"{message}; verified absent on OLT readback",
                data={"verified_absent": True},
            )
        except Exception as exc:
            logger.exception("SSH deauthorize_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH deauthorization failed: {exc}",
            )

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult:
        """Find ONT registration by serial via SSH CLI."""
        from app.services.network.olt_ssh_ont import find_ont_by_serial

        try:
            ok, message, entry = find_ont_by_serial(self._olt, serial_number)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"registration": entry} if entry is not None else {},
            )
        except Exception as exc:
            logger.exception("SSH find_ont_by_serial failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT lookup failed: {exc}",
            )

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult:
        """Update ONT profile binding via SSH CLI."""
        from app.services.network.olt_ssh import update_ont_profiles

        try:
            ok, message = update_ont_profiles(
                self._olt,
                fsp,
                ont_id,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH update_ont_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT profile update failed: {exc}",
            )

    def set_ont_description(
        self,
        fsp: str,
        ont_id: int,
        description: str,
    ) -> OltOperationResult:
        """Update an ONT's description via Huawei ``ont modify ... desc "..."``.

        Used by the reconciler's ``OltModifyDescription`` action — for the
        rare case where description drift is detected on an existing ONT
        (out-of-band edit, restored backup, etc.). For fresh authorizations,
        description is set via ``authorize_ont``'s ``desc`` parameter.
        """
        from app.services.network.olt_ssh import set_ont_description

        try:
            ok, message = set_ont_description(self._olt, fsp, ont_id, description)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH set_ont_description failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT description update failed: {exc}",
            )

    # ========== ONT Configuration ==========

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult:
        """Configure ONT IPHOST via SSH CLI."""
        from app.services.network.olt_ssh_ont.iphost import configure_ont_iphost

        try:
            ok, message = configure_ont_iphost(
                self._olt,
                fsp,
                ont_id,
                vlan_id=vlan,
                ip_index=ip_index,
                ip_mode=mode,
                priority=priority,
                ip_address=ip_address,
                subnet=subnet_mask,
                gateway=gateway,
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            return OltOperationResult.from_exception(
                exc,
                operation="SSH IPHOST configuration",
                logger_=logger,
            )

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult:
        """Bind TR-069 profile via SSH CLI."""
        from app.services.network.olt_ssh_ont import bind_tr069_server_profile

        try:
            ok, message = bind_tr069_server_profile(
                self._olt, fsp, ont_id, profile_id=profile_id
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH bind_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH TR-069 binding failed: {exc}",
            )

    # ========== Service Ports ==========

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        """Create service port via SSH CLI."""
        from app.services.network.olt_ssh_service_ports import (
            create_single_service_port,
        )

        try:
            ok, message, created_index = create_single_service_port(
                self._olt,
                fsp,
                ont_id,
                gem_index,
                vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
                port_index=port_index,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                data={
                    "port_index": created_index,
                    "service_port_index": created_index,
                }
                if created_index
                else {},
                service_port_index=created_index,
            )
        except Exception as exc:
            return OltOperationResult.from_exception(
                exc,
                operation="SSH service port creation",
                logger_=logger,
            )

    def delete_service_port(self, port_index: int) -> OltOperationResult:
        """Delete a service port and verify that it is absent on readback."""
        from app.services.network.olt_ssh_service_ports import (
            delete_service_port as ssh_delete,
        )
        from app.services.network.olt_ssh_service_ports import (
            get_service_port_by_index,
        )

        try:
            ok, message = ssh_delete(self._olt, port_index)
            if not ok:
                return OltOperationResult(success=False, message=message)

            read_ok, read_message, entry = get_service_port_by_index(
                self._olt, port_index
            )
            absent = (read_ok and entry is None) or (
                not read_ok
                and is_huawei_resource_absent(
                    read_message,
                    HuaweiCliResource.SERVICE_PORT,
                )
            )
            if not absent:
                detail = (
                    f"service-port {port_index} still exists"
                    if entry is not None
                    else f"absence readback failed: {read_message}"
                )
                return OltOperationResult(
                    success=False,
                    message=f"{message}; verification failed: {detail}",
                    data={"verified_absent": False},
                )
            return OltOperationResult(
                success=True,
                message=f"{message}; verified absent on OLT readback",
                data={"verified_absent": True},
            )
        except Exception as exc:
            return OltOperationResult.from_exception(
                exc,
                operation="SSH service port deletion",
                logger_=logger,
            )

    # ========== Batched Operations ==========

    def configure_management_batch(
        self,
        spec: BatchedMgmtSpec,
    ) -> OltOperationResult:
        """Execute batched management configuration in one SSH session."""
        from app.services.network.olt_batched_mgmt import (
            execute_batched_management_setup,
        )

        try:
            result = execute_batched_management_setup(self._olt, spec)
            return OltOperationResult(
                success=result.success,
                message=result.message,
                data={
                    "steps_completed": result.steps_completed,
                    "steps_failed": result.steps_failed,
                    "details": result.details,
                },
            )
        except Exception as exc:
            logger.exception("SSH configure_management_batch failed")
            return OltOperationResult(
                success=False,
                message=f"SSH batched management configuration failed: {exc}",
            )

    # ========== ONT Operations ==========

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Reboot ONT via SSH/OMCI."""
        from app.services.network.olt_ssh_ont.lifecycle import reboot_ont_omci

        try:
            ok, message = reboot_ont_omci(self._olt, fsp, ont_id)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH reboot_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT reboot failed: {exc}",
            )

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Factory reset ONT via SSH/OMCI."""
        from app.services.network.olt_ssh_ont.lifecycle import factory_reset_ont_omci

        try:
            ok, message = factory_reset_ont_omci(self._olt, fsp, ont_id)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH factory_reset_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT factory reset failed: {exc}",
            )

    # ========== Extended Configuration ==========

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Activate TCP stack on ONT management WAN via internet-config."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_internet_config,
        )

        try:
            ok, message = configure_ont_internet_config(
                self._olt, fsp, ont_id, ip_index=ip_index
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH configure_internet_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH internet-config failed: {exc}",
            )

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult:
        """Set route+NAT mode on ONT management WAN via wan-config."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_wan_config,
        )

        try:
            ok, message = configure_ont_wan_config(
                self._olt, fsp, ont_id, ip_index=ip_index, profile_id=profile_id
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH configure_wan_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH wan-config failed: {exc}",
            )

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        priority: int = 0,
        username: str,
        password: str,
    ) -> OltOperationResult:
        """Configure PPPoE WAN via OMCI (OLT-side, not TR-069)."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_pppoe_omci,
        )

        try:
            ok, message = configure_ont_pppoe_omci(
                self._olt,
                fsp,
                ont_id,
                ip_index=ip_index,
                vlan_id=vlan_id,
                priority=priority,
                username=username,
                password=password,
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH configure_pppoe failed")
            return OltOperationResult(
                success=False,
                message=f"SSH PPPoE configuration failed: {exc}",
            )

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult:
        """Set native VLAN on ONT Ethernet port for bridging mode."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_port_native_vlan,
        )

        try:
            ok, message = configure_ont_port_native_vlan(
                self._olt,
                fsp,
                ont_id,
                eth_port=eth_port,
                vlan_id=vlan_id,
                priority=priority,
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH configure_port_native_vlan failed")
            return OltOperationResult(
                success=False,
                message=f"SSH port native VLAN configuration failed: {exc}",
            )

    # ========== Cleanup Operations ==========

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT IP configuration for a given IP index."""
        from app.services.network.olt_ssh_ont.iphost import clear_ont_ipconfig

        try:
            ok, message = clear_ont_ipconfig(self._olt, fsp, ont_id, ip_index=ip_index)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH clear_iphost_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear iphost config failed: {exc}",
            )

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT internet-config state."""
        from app.services.network.olt_ssh_ont.omci_config import (
            clear_ont_internet_config,
        )

        try:
            ok, message = clear_ont_internet_config(
                self._olt, fsp, ont_id, ip_index=ip_index
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH clear_internet_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear internet config failed: {exc}",
            )

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT wan-config state."""
        from app.services.network.olt_ssh_ont.omci_config import (
            clear_ont_wan_config as ssh_clear,
        )

        try:
            ok, message = ssh_clear(self._olt, fsp, ont_id, ip_index=ip_index)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH clear_wan_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear WAN config failed: {exc}",
            )

    def unbind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        """Remove TR-069 server profile binding from ONT."""
        from app.services.network.olt_ssh_ont.tr069 import unbind_tr069_server_profile

        try:
            ok, message = unbind_tr069_server_profile(self._olt, fsp, ont_id)
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH unbind_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH unbind TR-069 profile failed: {exc}",
            )

    # ========== Read Operations ==========

    def get_service_ports(self, fsp: str) -> OltOperationResult:
        """Get all service-ports on a PON port."""
        from app.services.network import olt_ssh as core

        try:
            ok, message, entries = core.get_service_ports(self._olt, fsp)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"service_ports": entries},
            )
        except Exception as exc:
            return OltOperationResult.from_exception(
                exc,
                operation="SSH get service ports",
                logger_=logger,
                data={"service_ports": []},
            )

    def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Get service-ports for a specific ONT."""
        from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

        try:
            ok, message, entries = get_service_ports_for_ont(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"service_ports": entries},
            )
        except Exception as exc:
            return OltOperationResult.from_exception(
                exc,
                operation="SSH get service ports for ONT",
                logger_=logger,
                data={"service_ports": []},
            )

    def get_line_profiles(self) -> OltOperationResult:
        """Get line profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_line_profiles

        try:
            ok, message, entries = get_line_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
            )
        except Exception as exc:
            logger.exception("SSH get_line_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get line profiles failed: {exc}",
                data={"profiles": []},
            )

    def get_service_profiles(self) -> OltOperationResult:
        """Get service profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_service_profiles

        try:
            ok, message, entries = get_service_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
            )
        except Exception as exc:
            logger.exception("SSH get_service_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get service profiles failed: {exc}",
                data={"profiles": []},
            )

    def get_tr069_profiles(self) -> OltOperationResult:
        """Get TR-069 server profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_tr069_server_profiles

        try:
            ok, message, entries = get_tr069_server_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
            )
        except Exception as exc:
            logger.exception("SSH get_tr069_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get TR-069 profiles failed: {exc}",
                data={"profiles": []},
            )

    def get_tr069_profile_binding(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Get the TR-069 profile bound to one ONT via SSH CLI."""
        from app.services.network.olt_ssh_ont import get_tr069_server_profile_binding

        try:
            ok, message, profile_id = get_tr069_server_profile_binding(
                self._olt, fsp, ont_id
            )
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profile_id": profile_id},
            )
        except Exception as exc:
            logger.exception("SSH get_tr069_profile_binding failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get TR-069 binding failed: {exc}",
                data={"profile_id": None},
            )

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult:
        """Create a TR-069 server profile via SSH CLI."""
        from app.services.network.olt_ssh_profiles import create_tr069_server_profile

        try:
            ok, message = create_tr069_server_profile(
                self._olt,
                profile_name=profile_name,
                acs_url=acs_url,
                username=username,
                password=password,
                inform_interval=inform_interval,
            )
            return OltOperationResult(success=ok, message=message)
        except Exception as exc:
            logger.exception("SSH create_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH create TR-069 profile failed: {exc}",
            )

    def diagnose_service_ports(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Run diagnostics to troubleshoot service port state issues."""
        from app.services.network.olt_ssh_ont.diagnostics import (
            diagnose_service_ports as ssh_diagnose,
        )

        try:
            ok, message, diagnostics = ssh_diagnose(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"diagnostics": diagnostics},
            )
        except Exception as exc:
            logger.exception("SSH diagnose_service_ports failed")
            return OltOperationResult(
                success=False,
                message=f"SSH diagnose service ports failed: {exc}",
                data={"diagnostics": None},
            )

    def fetch_running_config(self) -> OltOperationResult:
        """Fetch full running config via SSH CLI."""
        from app.services.network.olt_ssh import fetch_running_config_ssh

        try:
            ok, message, config_text = fetch_running_config_ssh(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"config_text": config_text} if config_text else {},
            )
        except Exception as exc:
            logger.exception("SSH fetch_running_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH running-config fetch failed: {exc}",
            )


# ============================================================================
# Factory
# ============================================================================


def get_protocol_adapter(olt: OLTDevice) -> OltProtocolAdapterContract:
    """Get the protocol adapter for an OLT.

    Args:
        olt: OLT device instance

    Returns:
        OltProtocolAdapter instance
    """
    return OltProtocolAdapter(olt)


def get_protocol_adapter_from_config(
    config: OltConnectionConfig,
) -> OltProtocolAdapterContract:
    """Build an adapter from detached values without retaining an ORM entity."""

    return get_protocol_adapter(cast("OLTDevice", config))
