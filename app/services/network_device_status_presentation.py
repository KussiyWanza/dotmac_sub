"""Lifecycle-aware status presentation for the unified network-device surface."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.network_monitoring import NetworkDeviceLifecycleState
from app.schemas.status_presentation import StatusIcon, StatusPresentation, StatusTone
from app.services.device_operational_status import DeviceOperationalState
from app.services.status_presentation import device_operational_status_presentation


@dataclass(frozen=True, slots=True)
class NetworkDeviceListStatusContext:
    """Authoritative inputs to one device-list status presentation."""

    operational_status: DeviceOperationalState
    lifecycle_state: NetworkDeviceLifecycleState


def network_device_list_status_presentation(
    context: NetworkDeviceListStatusContext,
) -> StatusPresentation:
    """Present administrative lifecycle before active-device reachability.

    The returned value names the authoritative fact being presented. Archived
    remains the internal reversible-retirement state while its operator-facing
    label is Decommissioned. The raw binary operational status remains present
    separately on the device projection for diagnostics, filtering, and repair.
    """

    if context.lifecycle_state is NetworkDeviceLifecycleState.ARCHIVED:
        return StatusPresentation(
            value=NetworkDeviceLifecycleState.ARCHIVED.value,
            label="Decommissioned",
            tone=StatusTone.neutral,
            icon=StatusIcon.archive,
        )
    if context.lifecycle_state is NetworkDeviceLifecycleState.INACTIVE:
        return StatusPresentation(
            value=NetworkDeviceLifecycleState.INACTIVE.value,
            label="Inactive",
            tone=StatusTone.neutral,
            icon=StatusIcon.minus,
        )

    presentation = device_operational_status_presentation(context.operational_status)
    labels = {"working": "Online", "not_working": "Offline"}
    return presentation.model_copy(
        update={"label": labels.get(presentation.value, presentation.label)}
    )
