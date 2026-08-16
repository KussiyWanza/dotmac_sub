from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.network_monitoring import (
    AlertAcknowledgeRequest,
    AlertBulkAcknowledgeRequest,
    AlertBulkActionResponse,
    AlertBulkResolveRequest,
    AlertEventRead,
    AlertRead,
    AlertResolveRequest,
    AlertRuleBulkUpdateRequest,
    AlertRuleBulkUpdateResponse,
    AlertRuleCreate,
    AlertRuleRead,
    AlertRuleUpdate,
    DeviceInterfaceCreate,
    DeviceInterfaceRead,
    DeviceInterfaceUpdate,
    DeviceMetricCreate,
    DeviceMetricRead,
    NetworkDeviceCreate,
    NetworkDeviceRead,
    NetworkDeviceUpdate,
    PopSiteCreate,
    PopSiteRead,
    PopSiteUpdate,
    UptimeReportRequest,
    UptimeReportResponse,
)
from app.services import core_device_archive
from app.services import network_monitoring as monitoring_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


def _require_core_device_mutable(
    db: Session,
    *,
    device_id: str | UUID,
    mutation: core_device_archive.CoreDeviceMutation,
) -> None:
    core_device_archive.require_core_device_mutable(
        db,
        core_device_archive.RequireCoreDeviceMutableRequest(
            device_id=UUID(str(device_id)),
            mutation=mutation,
        ),
    )


def _with_device_operational_status(value):
    """Attach the read-only NOC projection to device API responses."""
    from app.services.device_operational_status import annotate_operational_status

    if isinstance(value, dict):
        devices = value.get("items", [])
    elif value is None:
        devices = []
    else:
        devices = [value]
    annotate_operational_status(devices)
    return value


@router.post(
    "/uptime-reports",
    response_model=UptimeReportResponse,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def generate_uptime_report(payload: UptimeReportRequest, db: Session = Depends(get_db)):
    return monitoring_service.uptime_report(db, payload)


@router.get(
    "/pop-sites",
    response_model=ListResponse[PopSiteRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_pop_sites(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.pop_sites.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/pop-sites",
    response_model=PopSiteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def create_pop_site(payload: PopSiteCreate, db: Session = Depends(get_db)):
    return monitoring_service.pop_sites.create_committed(db, payload)


@router.get(
    "/pop-sites/{site_id}",
    response_model=PopSiteRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_pop_site(site_id: str, db: Session = Depends(get_db)):
    return monitoring_service.pop_sites.get(db, site_id)


@router.patch(
    "/pop-sites/{site_id}",
    response_model=PopSiteRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def update_pop_site(
    site_id: str, payload: PopSiteUpdate, db: Session = Depends(get_db)
):
    return monitoring_service.pop_sites.update_committed(db, site_id, payload)


@router.delete(
    "/pop-sites/{site_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def delete_pop_site(site_id: str, db: Session = Depends(get_db)):
    monitoring_service.pop_sites.delete_committed(db, site_id)


@router.get(
    "/network-devices",
    response_model=ListResponse[NetworkDeviceRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_network_devices(
    pop_site_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return _with_device_operational_status(
        monitoring_service.network_devices.list_response(
            db, pop_site_id, is_active, order_by, order_dir, limit, offset
        )
    )


@router.post(
    "/network-devices",
    response_model=NetworkDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def create_network_device(payload: NetworkDeviceCreate, db: Session = Depends(get_db)):
    return _with_device_operational_status(
        monitoring_service.network_devices.create_committed(db, payload)
    )


@router.get(
    "/network-devices/{device_id}",
    response_model=NetworkDeviceRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_network_device(device_id: str, db: Session = Depends(get_db)):
    return _with_device_operational_status(
        monitoring_service.network_devices.get(db, device_id)
    )


@router.patch(
    "/network-devices/{device_id}",
    response_model=NetworkDeviceRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def update_network_device(
    device_id: str, payload: NetworkDeviceUpdate, db: Session = Depends(get_db)
):
    _require_core_device_mutable(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.EDIT,
    )
    return _with_device_operational_status(
        monitoring_service.network_devices.update_committed(db, device_id, payload)
    )


@router.delete(
    "/network-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def delete_network_device(device_id: str, db: Session = Depends(get_db)):
    _require_core_device_mutable(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.DEACTIVATE,
    )
    monitoring_service.network_devices.delete_committed(db, device_id)


@router.get(
    "/device-interfaces",
    response_model=ListResponse[DeviceInterfaceRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_device_interfaces(
    device_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.device_interfaces.list_response(
        db, device_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/device-interfaces",
    response_model=DeviceInterfaceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def create_device_interface(
    payload: DeviceInterfaceCreate, db: Session = Depends(get_db)
):
    _require_core_device_mutable(
        db,
        device_id=payload.device_id,
        mutation=core_device_archive.CoreDeviceMutation.INTERFACE_MONITORING,
    )
    return monitoring_service.device_interfaces.create_committed(db, payload)


@router.get(
    "/device-interfaces/{interface_id}",
    response_model=DeviceInterfaceRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_device_interface(interface_id: str, db: Session = Depends(get_db)):
    return monitoring_service.device_interfaces.get(db, interface_id)


@router.patch(
    "/device-interfaces/{interface_id}",
    response_model=DeviceInterfaceRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def update_device_interface(
    interface_id: str, payload: DeviceInterfaceUpdate, db: Session = Depends(get_db)
):
    interface = monitoring_service.device_interfaces.get(db, interface_id)
    _require_core_device_mutable(
        db,
        device_id=interface.device_id,
        mutation=core_device_archive.CoreDeviceMutation.INTERFACE_MONITORING,
    )
    return monitoring_service.device_interfaces.update_committed(
        db, interface_id, payload
    )


@router.delete(
    "/device-interfaces/{interface_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def delete_device_interface(interface_id: str, db: Session = Depends(get_db)):
    interface = monitoring_service.device_interfaces.get(db, interface_id)
    _require_core_device_mutable(
        db,
        device_id=interface.device_id,
        mutation=core_device_archive.CoreDeviceMutation.INTERFACE_MONITORING,
    )
    monitoring_service.device_interfaces.delete_committed(db, interface_id)


@router.get(
    "/device-metrics",
    response_model=ListResponse[DeviceMetricRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_device_metrics(
    device_id: str | None = None,
    interface_id: str | None = None,
    order_by: str = Query(default="recorded_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.device_metrics.list_response(
        db, device_id, interface_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/device-metrics",
    response_model=DeviceMetricRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def create_device_metric(payload: DeviceMetricCreate, db: Session = Depends(get_db)):
    return monitoring_service.device_metrics.create_committed(db, payload)


@router.get(
    "/device-metrics/{metric_id}",
    response_model=DeviceMetricRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_device_metric(metric_id: str, db: Session = Depends(get_db)):
    return monitoring_service.device_metrics.get(db, metric_id)


@router.delete(
    "/device-metrics/{metric_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def delete_device_metric(metric_id: str, db: Session = Depends(get_db)):
    monitoring_service.device_metrics.delete_committed(db, metric_id)


@router.get(
    "/alert-rules",
    response_model=ListResponse[AlertRuleRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_alert_rules(
    scope: str | None = None,
    severity: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alert_rules.list_response(
        db, scope, severity, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/alert-rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def create_alert_rule(payload: AlertRuleCreate, db: Session = Depends(get_db)):
    return monitoring_service.alert_rules.create_committed(db, payload)


@router.get(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_alert_rule(rule_id: str, db: Session = Depends(get_db)):
    return monitoring_service.alert_rules.get(db, rule_id)


@router.patch(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def update_alert_rule(
    rule_id: str, payload: AlertRuleUpdate, db: Session = Depends(get_db)
):
    return monitoring_service.alert_rules.update_committed(db, rule_id, payload)


@router.delete(
    "/alert-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def delete_alert_rule(rule_id: str, db: Session = Depends(get_db)):
    monitoring_service.alert_rules.delete_committed(db, rule_id)


@router.post(
    "/alert-rules/bulk/status",
    response_model=AlertRuleBulkUpdateResponse,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def bulk_update_alert_rules(
    payload: AlertRuleBulkUpdateRequest, db: Session = Depends(get_db)
):
    response = monitoring_service.alert_rules.bulk_update_response_committed(
        db, payload
    )
    return AlertRuleBulkUpdateResponse(**response)


@router.get(
    "/alerts",
    response_model=ListResponse[AlertRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_alerts(
    rule_id: str | None = None,
    device_id: str | None = None,
    interface_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    order_by: str = Query(default="triggered_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alerts.list_response(
        db,
        rule_id,
        device_id,
        interface_id,
        status,
        severity,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.get(
    "/alerts/{alert_id}",
    response_model=AlertRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def get_alert(alert_id: str, db: Session = Depends(get_db)):
    return monitoring_service.alerts.get(db, alert_id)


# NOTE: the static /alerts/bulk/* routes MUST be declared before the
# parametrized /alerts/{alert_id}/* routes — otherwise "bulk" is captured as an
# alert_id and the request 500s on a malformed-UUID lookup. Do not reorder.
@router.post(
    "/alerts/bulk/ack",
    response_model=AlertBulkActionResponse,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def bulk_acknowledge_alerts(
    payload: AlertBulkAcknowledgeRequest, db: Session = Depends(get_db)
):
    ack_payload = AlertAcknowledgeRequest(message=payload.message)
    response = monitoring_service.alerts.bulk_acknowledge_response_committed(
        db, [str(alert_id) for alert_id in payload.alert_ids], ack_payload
    )
    return AlertBulkActionResponse(**response)


@router.post(
    "/alerts/bulk/resolve",
    response_model=AlertBulkActionResponse,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def bulk_resolve_alerts(
    payload: AlertBulkResolveRequest, db: Session = Depends(get_db)
):
    resolve_payload = AlertResolveRequest(message=payload.message)
    response = monitoring_service.alerts.bulk_resolve_response_committed(
        db, [str(alert_id) for alert_id in payload.alert_ids], resolve_payload
    )
    return AlertBulkActionResponse(**response)


@router.post(
    "/alerts/{alert_id}/ack",
    response_model=AlertRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def acknowledge_alert(
    alert_id: str, payload: AlertAcknowledgeRequest, db: Session = Depends(get_db)
):
    return monitoring_service.alerts.acknowledge_committed(db, alert_id, payload)


@router.post(
    "/alerts/{alert_id}/resolve",
    response_model=AlertRead,
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def resolve_alert(
    alert_id: str, payload: AlertResolveRequest, db: Session = Depends(get_db)
):
    return monitoring_service.alerts.resolve_committed(db, alert_id, payload)


@router.get(
    "/alert-events",
    response_model=ListResponse[AlertEventRead],
    tags=["network-monitoring"],
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def list_alert_events(
    alert_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alert_events.list_response(
        db, alert_id, order_by, order_dir, limit, offset
    )
