"""Huawei OLT command grammar helpers."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.network import OLTDevice
from app.services.adapters.olt_types import OltCapabilities, olt_type_registry
from app.services.device_adapter_binding import AdapterBinding


@dataclass(frozen=True)
class HuaweiCommandProfile:
    name: str
    adapter_revision: str | None = None
    identity_fingerprint: str | None = None
    requires_slow_send: bool = True
    supports_slash_fsp_display: bool = False
    supports_scoped_autofind: bool = True

    def display_ont_info(self, fsp: str, ont_id: int) -> str:
        frame, slot, port = _split_fsp(fsp, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont info {frame}/{slot} {ont_id}"
            return f"display ont info {frame}/{slot} {port} {ont_id}"
        if port is None:
            raise ValueError("Full F/S/P is required for single-ONT display syntax")
        return f"display ont info {frame} {slot} {port} {ont_id}"

    def display_ont_optical_info(self, fsp: str, ont_id: int) -> str:
        frame, slot, port = _split_fsp(fsp, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont optical-info {frame}/{slot} {ont_id}"
            return f"display ont optical-info {frame}/{slot} {port} {ont_id}"
        if port is None:
            raise ValueError("Full F/S/P is required for single-ONT optical syntax")
        return f"display ont optical-info {frame} {slot} {port} {ont_id}"

    def display_ont_info_all(self, fsp_or_board: str) -> str:
        frame, slot, port = _split_fsp(fsp_or_board, allow_board=True)
        if self.supports_slash_fsp_display:
            if port is None:
                return f"display ont info {frame}/{slot} all"
            return f"display ont info {frame}/{slot} {port} all"
        if port is None:
            return f"display ont info {frame} {slot} all"
        return f"display ont info {frame} {slot} {port} all"

    def display_ont_status_inventory(self, fsp: str) -> str:
        """Return the bounded per-port status inventory command."""
        frame, slot, port = _split_fsp(fsp)
        if self.supports_slash_fsp_display:
            return f"display ont info summary {frame}/{slot}/{port}"
        return f"display ont info {frame} {slot} {port} all"


def _split_fsp(
    fsp: str,
    *,
    allow_board: bool = False,
) -> tuple[str, str, str | None]:
    parts = str(fsp or "").split("/")
    if len(parts) == 2 and allow_board:
        return parts[0], parts[1], None
    if len(parts) != 3:
        raise ValueError(f"Invalid F/S/P format: {fsp!r}")
    return parts[0], parts[1], parts[2]


def get_huawei_command_profile(olt: OLTDevice) -> HuaweiCommandProfile:
    model = str(getattr(olt, "model", None) or "").upper()
    firmware = getattr(olt, "firmware_version", None) or getattr(
        olt, "software_version", None
    )
    adapter = olt_type_registry.find(
        vendor=getattr(olt, "vendor", None),
        model=getattr(olt, "model", None),
        firmware=firmware,
    )
    capabilities = adapter.capabilities if adapter else OltCapabilities.conservative()
    binding = resolve_huawei_adapter_binding(olt)
    return HuaweiCommandProfile(
        name=capabilities.command_profile_name or "huawei-generic",
        adapter_revision=binding.adapter_revision if binding else None,
        identity_fingerprint=binding.identity.fingerprint if binding else None,
        requires_slow_send=capabilities.requires_slow_send,
        supports_slash_fsp_display=(
            capabilities.supports_slash_fsp_display or "MA5800" in model
        ),
        # MA5608T rejects ``display ont autofind <F/S/P>`` with
        # ``unknown_command``. Its global inventory command is supported and
        # callers filter the parsed observation back to the requested F/S/P.
        supports_scoped_autofind="MA5608T" not in model,
    )


def resolve_huawei_adapter_binding(olt: OLTDevice) -> AdapterBinding | None:
    """Resolve the code profile selected by the OLT's observed identity."""
    vendor = str(getattr(olt, "vendor", None) or "").strip()
    model = str(getattr(olt, "model", None) or "").strip()
    if not vendor or not model:
        return None
    firmware = getattr(olt, "firmware_version", None) or getattr(
        olt, "software_version", None
    )
    return olt_type_registry.resolve_binding(
        vendor=vendor,
        model=model,
        firmware=firmware,
        software_version=getattr(olt, "software_version", None),
    )
