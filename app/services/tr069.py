import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from fastapi import HTTPException
from sqlalchemy import and_, case, false, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.network import CPEDevice, OntUnit
from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Job,
    Tr069JobStatus,
    Tr069Parameter,
    Tr069Session,
)
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceCreate,
    Tr069CpeDeviceUpdate,
    Tr069ParameterCreate,
    Tr069ParameterUpdate,
    Tr069SessionCreate,
    Tr069SessionUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.credential_crypto import encrypt_credential
from app.services.genieacs_client import (
    GenieACSClient,
    GenieACSError,
    create_genieacs_client,
    normalize_tr069_serial,
)
from app.services.network import cpe as cpe_service
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_desired_config import (
    desired_config,
    get_desired_config_value,
)
from app.services.network.ont_serials import (
    ActiveOntSerialIndex,
    build_active_ont_serial_index,
    find_unique_active_ont_by_serial,
)
from app.services.network.ont_status import (
    apply_status_snapshot,
    resolve_acs_online_window_minutes_for_model,
    resolve_ont_status_snapshot,
)
from app.services.network.serial_utils import search_candidates
from app.services.response import ListResponseMixin

_ACS_CREDENTIAL_FIELDS = ("cwmp_password", "connection_request_password")
_STALE_INFORM_SERVICE_APPLY_DAYS = 5
# Commit the GenieACS device-sync upsert loop every N devices so a large ACS
# inventory doesn't accumulate one long write transaction (row locks +
# delayed autovacuum). Each device upsert is independent, so partial progress
# on failure is fine.
_GENIEACS_SYNC_COMMIT_BATCH = 200
# Commit the auto-link pass in batches too, for the same reason and so a
# soft-time-limit interrupt mid-pass keeps the links it already made.
_GENIEACS_AUTOLINK_COMMIT_BATCH = 200


def _safe_rollback(db: Session) -> None:
    """Best-effort rollback that tolerates a connection left mid-statement.

    A Celery ``SoftTimeLimitExceeded`` is raised asynchronously and can land
    while psycopg is mid-query, leaving the connection in a state where even
    ``rollback()`` raises ("another command is already in progress"). Swallow
    that here so the original interrupt propagates cleanly; the session's
    pool-return reset (on ``db.close()``) invalidates the bad connection.
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        logger.warning(
            "rollback after interrupt failed; connection will be reset on close"
        )


logger = logging.getLogger(__name__)


def resolve_acs_server_for_ont(
    db: Session,
    *,
    ont: OntUnit | None = None,
    olt_id: str | None = None,
) -> str | None:
    """Resolve the desired ACS server ID for an ONT or OLT context."""
    from app.services.network.acs_resolution import (
        resolve_acs_for_ont,
        resolve_operational_acs,
    )

    if ont is not None:
        return resolve_acs_for_ont(db, ont, olt_id=olt_id).server_id
    return resolve_operational_acs(db, olt_id=olt_id).server_id


def _job_extra(
    job: Tr069Job,
    *,
    device: Tr069CpeDevice | None = None,
    server: Tr069AcsServer | None = None,
    task: dict | None = None,
    result: object | None = None,
    error: str | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "tr069_job",
        "job_id": str(job.id),
        "job_name": job.name,
        "job_status": job.status.value,
        "command": job.command,
        "device_id": str(job.device_id),
    }
    if device is not None:
        extra["serial_number"] = device.serial_number
        extra["acs_server_id"] = (
            str(device.acs_server_id) if device.acs_server_id else None
        )
        extra["genieacs_device_id"] = device.genieacs_device_id
    if server is not None:
        extra["acs_server_name"] = server.name
        extra["acs_base_url"] = server.base_url
    if task is not None:
        extra["task"] = task
    if result is not None:
        extra["result"] = result
    if error is not None:
        extra["error"] = error
    return extra


def _normalized_serial_expr(column):
    """Build a SQL expression that strips common serial formatting."""
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _json_safe(value: Any) -> Any:
    """Return a JSON-column-safe representation without losing structure."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _first_text(*values: object | None, max_len: int | None = None) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        return text[:max_len] if max_len else text
    return None


def _payload_lookup(payload: dict[str, Any], *keys: str) -> Any:
    """Case-insensitive top-level lookup for mixed ACS webhook shapes."""
    if not isinstance(payload, dict):
        return None
    wanted = {key.lower() for key in keys}
    for key, value in payload.items():
        if str(key).lower() in wanted:
            return value
    return None


def _extract_serial_from_device_id(device_id_raw: str | None) -> str | None:
    device_id_str = (device_id_raw or "").strip()
    if not device_id_str:
        return None
    parts = device_id_str.split("-", 2)
    if len(parts) == 3:
        return parts[2].strip() or None
    return None


def _normalize_event_value(event: Any) -> tuple[str, Any]:
    """Convert common CWMP/GenieACS event shapes into our enum token."""
    raw_event = event
    if isinstance(event, list):
        labels: list[str] = []
        for item in event:
            if isinstance(item, dict):
                code = _first_text(
                    item.get("EventCode"),
                    item.get("event_code"),
                    item.get("code"),
                    item.get("_value"),
                )
                if code:
                    labels.append(code)
            else:
                text = _first_text(item)
                if text:
                    labels.append(text)
        event_text = " ".join(labels) if labels else "periodic"
    elif isinstance(event, dict):
        event_text = (
            _first_text(
                event.get("EventCode"),
                event.get("event_code"),
                event.get("code"),
                event.get("_value"),
                event,
            )
            or "periodic"
        )
    else:
        event_text = _first_text(event) or "periodic"

    normalized = event_text.strip().lower().replace("-", "_")
    if "0 bootstrap" in normalized or normalized == "bootstrap":
        return "bootstrap", raw_event
    if "1 boot" in normalized or normalized == "boot":
        return "boot", raw_event
    if "2 periodic" in normalized or normalized == "periodic":
        return "periodic", raw_event
    if "4 value change" in normalized or "value_change" in normalized:
        return "value_change", raw_event
    if "6 connection request" in normalized or "connection_request" in normalized:
        return "connection_request", raw_event
    if "7 transfer complete" in normalized or "transfer_complete" in normalized:
        return "transfer_complete", raw_event
    if "8 diagnostics complete" in normalized or "diagnostics_complete" in normalized:
        return "diagnostics_complete", raw_event
    return normalized or "periodic", raw_event


def _parameter_value(value: Any) -> str | None:
    if isinstance(value, dict):
        if "_value" in value:
            value = value.get("_value")
        elif "value" in value:
            value = value.get("value")
        elif "Value" in value:
            value = value.get("Value")
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return str(_json_safe(value))
    return str(value)


def _collect_parameters_from_mapping(
    source: dict[str, Any],
    output: dict[str, str | None],
) -> None:
    for name, value in source.items():
        key = str(name).strip()
        if not key or len(key) > 255:
            continue
        if not (
            key.startswith("Device.")
            or key.startswith("InternetGatewayDevice.")
            or key.startswith("X_")
        ):
            continue
        output[key[:255]] = _parameter_value(value)


def _extract_inform_parameters(
    raw_payload: dict[str, Any] | None,
) -> dict[str, str | None]:
    """Extract reported parameter values from common inform webhook formats."""
    if not isinstance(raw_payload, dict):
        return {}
    params: dict[str, str | None] = {}

    for key in ("parameters", "Parameters", "params", "parameter_values"):
        value = raw_payload.get(key)
        if isinstance(value, dict):
            _collect_parameters_from_mapping(value, params)

    parameter_list = raw_payload.get("ParameterList") or raw_payload.get(
        "parameter_list"
    )
    if isinstance(parameter_list, list):
        for item in parameter_list:
            if not isinstance(item, dict):
                continue
            name = _first_text(item.get("Name"), item.get("name"), max_len=255)
            if name:
                params[name] = _parameter_value(
                    item.get("Value", item.get("value", item.get("_value")))
                )
    elif isinstance(parameter_list, dict):
        _collect_parameters_from_mapping(parameter_list, params)

    _collect_parameters_from_mapping(raw_payload, params)
    return params


def _upsert_inform_parameters(
    db: Session,
    *,
    device: Tr069CpeDevice,
    parameters: dict[str, str | None],
    updated_at: datetime,
) -> int:
    if not parameters:
        return 0
    names = list(parameters.keys())
    existing = {
        param.name: param
        for param in db.query(Tr069Parameter)
        .filter(Tr069Parameter.device_id == device.id)
        .filter(Tr069Parameter.name.in_(names))
        .all()
    }
    changed = 0
    for name, value in parameters.items():
        param = existing.get(name)
        if param:
            param.value = value
            param.updated_at = updated_at
        else:
            db.add(
                Tr069Parameter(
                    device_id=device.id,
                    name=name,
                    value=value,
                    updated_at=updated_at,
                )
            )
        changed += 1
    return changed


def _ont_has_saved_service_intent(db: Session, ont_id: object) -> bool:
    from app.models.network import OntAssignment, OntUnit, OntWanServiceInstance

    ont = db.get(OntUnit, ont_id)
    if ont is None or not ont.is_active:
        return False
    # Service delivery is assignment-owned. In particular, a temporary
    # management-only commissioning intent must never make config-pack defaults
    # look like customer WAN/Wi-Fi intent on Inform.
    assignment_id = db.scalar(
        select(OntAssignment.id)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .limit(1)
    )
    if assignment_id is None:
        return False
    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    if (
        get_desired_config_value(desired_config(ont), "delivery", "pending_apply")
        or effective_values.get("pppoe_username")
        or effective_values.get("wan_static_ip")
        or effective_values.get("wifi_ssid")
        or effective_values.get("wifi_password")
        or effective_values.get("wifi_enabled") is not None
        or effective_values.get("wifi_channel")
        or effective_values.get("wifi_security_mode")
        or effective_values.get("lan_ip")
        or effective_values.get("lan_subnet")
        or effective_values.get("lan_dhcp_enabled") is not None
        or effective_values.get("lan_dhcp_start")
        or effective_values.get("lan_dhcp_end")
        or effective_values.get("wan_mode")
        or effective_values.get("wan_vlan")
    ):
        return True
    return bool(
        db.scalar(
            select(OntWanServiceInstance.id)
            .where(OntWanServiceInstance.ont_id == ont.id)
            .where(OntWanServiceInstance.is_active.is_(True))
            .limit(1)
        )
    )


def _normalize_utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _queue_saved_service_apply_after_stale_inform(
    db: Session,
    *,
    ont_id: object | None,
    previous_last_inform_at: datetime | None,
    now: datetime,
) -> bool:
    if ont_id is None:
        return False
    current = _normalize_utc_timestamp(now) or datetime.now(UTC)
    previous = _normalize_utc_timestamp(previous_last_inform_at)
    ont = db.get(OntUnit, ont_id)
    config = desired_config(ont) if ont else {}
    pending_apply = bool(get_desired_config_value(config, "delivery", "pending_apply"))
    stale_cutoff = current - timedelta(days=_STALE_INFORM_SERVICE_APPLY_DAYS)
    if not pending_apply and previous is not None and previous >= stale_cutoff:
        return False
    if not _ont_has_saved_service_intent(db, ont_id):
        return False

    from app.services.queue_adapter import enqueue_task

    dispatch = enqueue_task(
        "app.tasks.tr069.apply_saved_ont_service_config",
        args=[str(ont_id), "stale_inform_reconnect"],
        correlation_id=f"ont_service_reconnect:{ont_id}",
        source="tr069_inform",
        countdown=30,
    )
    return dispatch.queued


def _resolve_default_acs_server(db: Session) -> Tr069AcsServer | None:
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    if default_server_id:
        server = db.get(Tr069AcsServer, str(default_server_id))
        if server and server.is_active:
            return server
    return (
        db.query(Tr069AcsServer)
        .filter(Tr069AcsServer.is_active.is_(True))
        .order_by(Tr069AcsServer.created_at.asc())
        .first()
    )


def _find_matching_ont_for_serial(
    db: Session,
    serial: str | None,
    *,
    serial_index: ActiveOntSerialIndex | None = None,
) -> OntUnit | None:
    if not serial:
        return None

    ont = (
        serial_index.find_unique(serial)
        if serial_index is not None
        else find_unique_active_ont_by_serial(db, serial)
    )
    if ont is None:
        logger.info(
            "TR-069 serial %s did not resolve to a unique active ONT; leaving unlinked",
            serial,
        )
    return ont


def _link_unassigned_inform_device_to_matching_ont(
    db: Session,
    device: Tr069CpeDevice,
    *,
    serial: str | None,
    serial_index: ActiveOntSerialIndex | None = None,
) -> None:
    """Link an existing unassigned TR-069 device to a unique ONT serial match."""
    if getattr(device, "ont_unit_id", None):
        return
    ont = _find_matching_ont_for_serial(
        db,
        serial or device.serial_number,
        serial_index=serial_index,
    )
    if ont is None:
        return
    link_tr069_device_to_ont(db, device, ont, acs_server_id=device.acs_server_id)


def _resolve_device_for_inform(
    db: Session,
    *,
    serial: str | None,
    device_id_raw: str | None,
    acs_server_id: str | None,
    oui: str | None,
    product_class: str | None,
) -> Tr069CpeDevice | None:
    device_id_str = (device_id_raw or "").strip()
    normalized_candidates = [
        normalize_tr069_serial(candidate) for candidate in search_candidates(serial)
    ]
    normalized_candidates = [
        candidate for candidate in normalized_candidates if candidate
    ]

    query = db.query(Tr069CpeDevice).filter(Tr069CpeDevice.is_active.is_(True))
    if acs_server_id:
        query = query.filter(Tr069CpeDevice.acs_server_id == acs_server_id)

    if device_id_str:
        found = query.filter(Tr069CpeDevice.genieacs_device_id == device_id_str).first()
        if found:
            return found

    if serial:
        found = query.filter(Tr069CpeDevice.serial_number == serial).first()
        if found:
            return found

    if normalized_candidates:
        found = query.filter(
            _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(
                normalized_candidates
            )
        ).first()
        if found:
            return found

    server = db.get(Tr069AcsServer, str(acs_server_id)) if acs_server_id else None
    if not server:
        server = _resolve_default_acs_server(db)
    if not server:
        return None

    device = Tr069CpeDevice(
        acs_server_id=server.id,
        serial_number=_first_text(serial, max_len=120),
        oui=_first_text(oui, max_len=8),
        product_class=_first_text(product_class, max_len=120),
        genieacs_device_id=_first_text(device_id_str, max_len=255),
        is_active=True,
    )
    db.add(device)

    ont = _find_matching_ont_for_serial(db, serial)
    if ont:
        link_tr069_device_to_ont(db, device, ont, acs_server_id=server.id)

    return device


def _update_genieacs_device_id_from_inform(
    db: Session,
    device: Tr069CpeDevice,
    device_id_str: str,
) -> None:
    clean_id = _first_text(device_id_str, max_len=255)
    if not clean_id or device.genieacs_device_id == clean_id:
        return

    owner = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.genieacs_device_id == clean_id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .filter(Tr069CpeDevice.id != device.id)
        .first()
    )
    if owner:
        logger.info(
            "Inform for TR-069 device %s reported GenieACS id %s already owned by "
            "active TR-069 device %s; leaving current id %s",
            device.id,
            clean_id,
            owner.id,
            device.genieacs_device_id,
        )
        return

    logger.info(
        "Updating TR-069 device %s GenieACS id from %s to %s after inform",
        device.id,
        device.genieacs_device_id,
        clean_id,
    )
    device.genieacs_device_id = clean_id


def _validate_target_cpe_device(
    db: Session,
    *,
    cpe_device_id,
) -> CPEDevice | None:
    if not cpe_device_id:
        return None
    cpe = db.get(CPEDevice, cpe_device_id)
    if cpe is None:
        raise HTTPException(status_code=404, detail="CPE device not found")
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)
    if (
        inventory_subscriber_id is not None
        and getattr(cpe, "subscriber_id", None) == inventory_subscriber_id
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot link TR-069 device to parked inventory CPE",
        )
    return cpe


def sync_ont_acs_server(
    db: Session,
    ont,
    acs_server_id,
) -> int:
    """Keep linked TR-069 rows aligned without rewriting ONT ACS policy."""
    if not acs_server_id:
        return 0
    linked_devices = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.ont_unit_id == ont.id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .all()
    )
    updated = 0
    for linked_device in linked_devices:
        if linked_device.acs_server_id != acs_server_id:
            linked_device.acs_server_id = acs_server_id
            updated += 1
    return updated


def refresh_ont_status_snapshot(
    db: Session,
    ont,
) -> None:
    """Recompute ACS/effective status from the ONT's current active TR-069 links."""
    acs_last_inform_at = (
        db.query(func.max(Tr069CpeDevice.last_inform_at))
        .filter(Tr069CpeDevice.ont_unit_id == ont.id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .scalar()
    )
    snapshot = resolve_ont_status_snapshot(
        olt_status=getattr(ont, "olt_status", None),
        acs_last_inform_at=acs_last_inform_at,
        online_window_minutes=resolve_acs_online_window_minutes_for_model(ont),
    )
    apply_status_snapshot(ont, snapshot)


def _refresh_synced_ont_acs_observations(
    db: Session,
    *,
    acs_server_id: str,
) -> int:
    """Project the latest synced ACS observation onto each linked ONT.

    Build the statement with SQLAlchemy so null-safe change detection compiles
    for both PostgreSQL (``IS DISTINCT FROM``) and SQLite (``IS NOT``). The
    latter is the canonical unit-test database and must exercise the same
    transaction boundary instead of rolling back the completed sync.
    """
    latest = (
        select(
            Tr069CpeDevice.ont_unit_id.label("ont_unit_id"),
            func.max(Tr069CpeDevice.last_inform_at).label("last_inform_at"),
        )
        .where(
            Tr069CpeDevice.acs_server_id == acs_server_id,
            Tr069CpeDevice.is_active.is_(True),
            Tr069CpeDevice.ont_unit_id.is_not(None),
            Tr069CpeDevice.last_inform_at.is_not(None),
        )
        .group_by(Tr069CpeDevice.ont_unit_id)
        .cte("latest")
    )
    statement = (
        update(OntUnit)
        .where(
            OntUnit.id == latest.c.ont_unit_id,
            OntUnit.acs_last_inform_at.is_distinct_from(latest.c.last_inform_at),
        )
        .values(
            acs_last_inform_at=latest.c.last_inform_at,
            last_seen_at=case(
                (
                    or_(
                        OntUnit.last_seen_at.is_(None),
                        OntUnit.last_seen_at < latest.c.last_inform_at,
                    ),
                    latest.c.last_inform_at,
                ),
                else_=OntUnit.last_seen_at,
            ),
            updated_at=func.current_timestamp(),
        )
    )
    result = db.execute(statement)
    # SQLAlchemy `Result` is the generic supertype; UPDATE statements return a
    # `CursorResult`, which carries `.rowcount`.
    return result.rowcount or 0


def link_tr069_device_to_ont(
    db: Session,
    device: Tr069CpeDevice,
    ont,
    *,
    acs_server_id=None,
) -> None:
    """Enforce a single active TR-069 link per ONT.

    The linked TR-069 row is observed ACS state. Linking must not rewrite the
    ONT's desired ACS override/inheritance policy.
    """
    if getattr(ont, "id", None) is None:
        raise ValueError("ONT must be persisted before linking TR-069 devices")
    previous_ont_id = getattr(device, "ont_unit_id", None)

    other_links = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.ont_unit_id == ont.id)
        .filter(Tr069CpeDevice.id != device.id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .all()
    )
    for other in other_links:
        other.ont_unit_id = None
        if device.genieacs_device_id and not other.genieacs_device_id:
            if device.cpe_device_id is None and other.cpe_device_id is not None:
                device.cpe_device_id = other.cpe_device_id
            other.is_active = False
    if other_links:
        db.flush()

    device.ont_unit_id = ont.id
    target_acs_server_id = acs_server_id or device.acs_server_id
    if target_acs_server_id and device.acs_server_id != target_acs_server_id:
        device.acs_server_id = target_acs_server_id
    db.flush()
    refresh_ont_status_snapshot(db, ont)

    if previous_ont_id and previous_ont_id != ont.id:
        from app.models.network import OntUnit

        previous_ont = db.get(OntUnit, previous_ont_id)
        if previous_ont:
            refresh_ont_status_snapshot(db, previous_ont)


class AcsServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069AcsServerCreate):
        data = payload.model_dump()
        for field in _ACS_CREDENTIAL_FIELDS:
            if data.get(field):
                data[field] = encrypt_credential(data[field])
        server = Tr069AcsServer(**data)
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get(db: Session, server_id: str):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        return server

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069AcsServer)
        if is_active is None:
            query = query.filter(Tr069AcsServer.is_active.is_(True))
        else:
            query = query.filter(Tr069AcsServer.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Tr069AcsServer.created_at, "name": Tr069AcsServer.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, server_id: str, payload: Tr069AcsServerUpdate):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        data = payload.model_dump(exclude_unset=True)
        for field in _ACS_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])
        for key, value in data.items():
            setattr(server, key, value)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: str):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        server.is_active = False
        db.commit()


class CpeDevices(ListResponseMixin):
    _GENIEACS_SYNC_PROJECTION = ",".join(
        (
            "_id",
            "_deviceId",
            "_lastInform",
            "Device.DeviceInfo.SerialNumber",
            "Device.DeviceInfo.ProductClass",
            "Device.ManagementServer.ConnectionRequestURL",
            "InternetGatewayDevice.DeviceInfo.SerialNumber",
            "InternetGatewayDevice.DeviceInfo.ProductClass",
            "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
        )
    )

    @staticmethod
    def _clip_text(value: object | None, max_len: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:max_len]

    @staticmethod
    def _extract_identity(
        client: GenieACSClient, device_data: dict
    ) -> tuple[str | None, str | None, str | None]:
        device_id = str(device_data.get("_id") or "").strip()
        parsed_oui: str | None = None
        parsed_product_class: str | None = None
        parsed_serial: str | None = None

        if device_id:
            try:
                parsed_oui, parsed_product_class, parsed_serial = (
                    client.parse_device_id(device_id)
                )
            except ValueError:
                logger.warning("Invalid device ID format: %s", device_id)

        raw_device_id = device_data.get("_deviceId")
        fallback_oui = fallback_product_class = fallback_serial = None
        if isinstance(raw_device_id, dict):
            fallback_oui = raw_device_id.get("_OUI") or raw_device_id.get("OUI")
            fallback_product_class = raw_device_id.get(
                "_ProductClass"
            ) or raw_device_id.get("ProductClass")
            fallback_serial = raw_device_id.get("_SerialNumber") or raw_device_id.get(
                "SerialNumber"
            )

        param_serial = client.extract_parameter_value(
            device_data, "Device.DeviceInfo.SerialNumber"
        ) or client.extract_parameter_value(
            device_data, "InternetGatewayDevice.DeviceInfo.SerialNumber"
        )
        param_product_class = client.extract_parameter_value(
            device_data, "Device.DeviceInfo.ProductClass"
        ) or client.extract_parameter_value(
            device_data, "InternetGatewayDevice.DeviceInfo.ProductClass"
        )

        # Prefer structured GenieACS identity fields over parsed `_id` parts.
        oui = CpeDevices._clip_text(fallback_oui, 8) or CpeDevices._clip_text(
            parsed_oui, 8
        )
        product_class = (
            CpeDevices._clip_text(param_product_class, 120)
            or CpeDevices._clip_text(fallback_product_class, 120)
            or CpeDevices._clip_text(parsed_product_class, 120)
        )
        serial_number = (
            CpeDevices._clip_text(param_serial, 120)
            or CpeDevices._clip_text(fallback_serial, 120)
            or CpeDevices._clip_text(parsed_serial, 120)
        )
        return oui, product_class, serial_number

    @staticmethod
    def _find_inactive_device_for_ont(
        db: Session,
        *,
        acs_server_id: str,
        ont,
    ) -> Tr069CpeDevice | None:
        serial_candidates = [
            normalize_tr069_serial(candidate)
            for candidate in search_candidates(getattr(ont, "serial_number", None))
        ]
        serial_candidates = [candidate for candidate in serial_candidates if candidate]

        query = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
            .filter(Tr069CpeDevice.is_active.is_(False))
            .filter(
                or_(
                    Tr069CpeDevice.ont_unit_id == ont.id,
                    (
                        _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(
                            serial_candidates
                        )
                        if serial_candidates
                        else false()
                    ),
                )
            )
            .order_by(
                Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc()
            )
        )
        return query.first()

    @staticmethod
    def ensure_local_ont_devices(db: Session, acs_server_id: str) -> dict[str, int]:
        """Ensure local ONTs assigned to an ACS have active TR-069 rows.

        GenieACS only returns devices it knows about. This pass keeps local ONTs
        visible in TR-069 management even before a first inform, or after an
        inactive placeholder exists for an offline device.
        """
        server = db.get(Tr069AcsServer, acs_server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")

        from app.models.network import OLTDevice, OntUnit

        onts = (
            db.query(OntUnit)
            .outerjoin(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
            .filter(OntUnit.is_active.is_(True))
            .filter(
                or_(
                    OntUnit.tr069_acs_server_id == server.id,
                    and_(
                        OntUnit.tr069_acs_server_id.is_(None),
                        OLTDevice.tr069_acs_server_id == server.id,
                    ),
                )
            )
            .all()
        )

        created = 0
        reactivated = 0
        linked = 0
        for ont in onts:
            active_device = (
                db.query(Tr069CpeDevice)
                .filter(Tr069CpeDevice.ont_unit_id == ont.id)
                .filter(Tr069CpeDevice.is_active.is_(True))
                .first()
            )
            if active_device:
                if active_device.acs_server_id != server.id:
                    active_device.acs_server_id = server.id
                    linked += 1
                sync_ont_acs_server(db, ont, server.id)
                continue

            device = CpeDevices._find_inactive_device_for_ont(
                db,
                acs_server_id=str(server.id),
                ont=ont,
            )
            if device:
                device.is_active = True
                device.serial_number = str(
                    ont.serial_number or device.serial_number or ""
                )[:120]
                reactivated += 1
            else:
                device = Tr069CpeDevice(
                    acs_server_id=server.id,
                    serial_number=(ont.serial_number or "")[:120],
                    is_active=True,
                )
                db.add(device)
                created += 1

            link_tr069_device_to_ont(
                db,
                device,
                ont,
                acs_server_id=server.id,
            )
            linked += 1
            apply_status_snapshot(
                ont,
                resolve_ont_status_snapshot(
                    olt_status=getattr(ont, "olt_status", None),
                    acs_last_inform_at=device.last_inform_at,
                    online_window_minutes=(
                        resolve_acs_online_window_minutes_for_model(ont)
                    ),
                ),
            )

        if created or reactivated or linked:
            db.commit()

        return {
            "local_onts_checked": len(onts),
            "local_created": created,
            "local_reactivated": reactivated,
            "local_linked": linked,
        }

    @staticmethod
    def create(db: Session, payload: Tr069CpeDeviceCreate):
        payload_data = payload.model_dump()
        _validate_target_cpe_device(
            db,
            cpe_device_id=payload_data.get("cpe_device_id"),
        )
        device = Tr069CpeDevice(**payload_data)
        db.add(device)
        if device.ont_unit_id:
            from app.models.network import OntUnit

            ont = db.get(OntUnit, device.ont_unit_id)
            if ont:
                link_tr069_device_to_ont(
                    db,
                    device,
                    ont,
                    acs_server_id=device.acs_server_id,
                )
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        acs_server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069CpeDevice)
        if acs_server_id:
            query = query.filter(Tr069CpeDevice.acs_server_id == acs_server_id)
        if is_active is None:
            query = query.filter(Tr069CpeDevice.is_active.is_(True))
        else:
            query = query.filter(Tr069CpeDevice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Tr069CpeDevice.created_at,
                "serial_number": Tr069CpeDevice.serial_number,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: Tr069CpeDeviceUpdate):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        update_data = payload.model_dump(exclude_unset=True)
        target_cpe_id = update_data.get("cpe_device_id", device.cpe_device_id)
        _validate_target_cpe_device(db, cpe_device_id=target_cpe_id)
        target_ont_id = update_data.get("ont_unit_id", device.ont_unit_id)
        for key, value in update_data.items():
            setattr(device, key, value)
        if target_ont_id:
            from app.models.network import OntUnit

            ont = db.get(OntUnit, target_ont_id)
            if ont:
                link_tr069_device_to_ont(
                    db,
                    device,
                    ont,
                    acs_server_id=device.acs_server_id,
                )
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        device.is_active = False
        db.commit()

    @staticmethod
    def sync_from_genieacs(db: Session, acs_server_id: str) -> dict:
        """Sync devices from GenieACS to local database.

        Args:
            db: Database session
            acs_server_id: ACS server ID to sync from

        Returns:
            Dict with created and updated counts
        """
        server = db.get(Tr069AcsServer, acs_server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        base_url = server.base_url
        # Release the read transaction opened by the lookup above BEFORE the
        # (potentially slow) GenieACS HTTP fetch, so the DB connection isn't left
        # "idle in transaction" across the network round-trip. On a busy ACS that
        # hold lasted ~a minute, pinning a pool slot and blocking autovacuum. The
        # device upserts below run in their own transaction (committed as before).
        db.commit()

        try:
            client = create_genieacs_client(base_url)
            devices = client.list_devices(
                projection=CpeDevices._GENIEACS_SYNC_PROJECTION
            )
        except GenieACSError as e:
            raise HTTPException(status_code=502, detail=f"GenieACS error: {e}")

        # A full ACS sync may process thousands of devices.  Resolve ONT serial
        # identity from one authoritative snapshot instead of querying every
        # active ONT once per CPE (and again in the auto-link pass).
        serial_index = build_active_ont_serial_index(db)

        created, updated = 0, 0
        datetime.now(UTC)

        for idx, device_data in enumerate(devices, start=1):
            # Extract GenieACS device ID (the authoritative identifier)
            genieacs_device_id = str(device_data.get("_id") or "").strip()
            if not genieacs_device_id:
                logger.warning("Skipping GenieACS device without _id")
                continue

            oui, product_class, serial_number = CpeDevices._extract_identity(
                client, device_data
            )
            if not serial_number:
                logger.warning(
                    "Skipping GenieACS device without serial number: %s",
                    genieacs_device_id,
                )
                continue

            # Skip GenieACS discovery service probes — these are not real devices
            if oui == "DISCOVERYSERVICE" or product_class == "DISCOVERYSERVICE":
                continue

            # Extract connection request URL if available
            connection_url = client.extract_parameter_value(
                device_data, "Device.ManagementServer.ConnectionRequestURL"
            ) or client.extract_parameter_value(
                device_data,
                "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
            )
            connection_url = CpeDevices._clip_text(connection_url, 255)

            normalized_serial = normalize_tr069_serial(serial_number)

            # Primary lookup: by GenieACS device ID (stable, authoritative)
            existing = (
                db.query(Tr069CpeDevice)
                .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                .filter(Tr069CpeDevice.genieacs_device_id == genieacs_device_id)
                .first()
            )
            # Fallback: by exact serial number
            if not existing:
                existing = (
                    db.query(Tr069CpeDevice)
                    .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                    .filter(Tr069CpeDevice.serial_number == serial_number)
                    .first()
                )
            # Fallback: match using normalized serials to tolerate vendor formatting differences.
            if not existing and normalized_serial:
                existing = (
                    db.query(Tr069CpeDevice)
                    .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                    .filter(
                        _normalized_serial_expr(Tr069CpeDevice.serial_number)
                        == normalized_serial
                    )
                    .first()
                )
            # Fallback: update legacy/mis-parsed records by stable connection URL.
            if not existing and connection_url:
                existing = (
                    db.query(Tr069CpeDevice)
                    .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                    .filter(Tr069CpeDevice.connection_request_url == connection_url)
                    .first()
                )

            # Extract last inform time
            last_inform = device_data.get("_lastInform")
            last_inform_at = None
            if last_inform:
                try:
                    last_inform_at = datetime.fromisoformat(
                        last_inform.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            if existing:
                existing.genieacs_device_id = genieacs_device_id
                existing.oui = oui
                existing.product_class = product_class
                existing.connection_request_url = connection_url
                existing.last_inform_at = last_inform_at
                existing.is_active = True
                updated += 1
            else:
                new_device = Tr069CpeDevice(
                    acs_server_id=server.id,
                    genieacs_device_id=genieacs_device_id,
                    serial_number=serial_number,
                    oui=oui,
                    product_class=product_class,
                    connection_request_url=connection_url,
                    last_inform_at=last_inform_at,
                    is_active=True,
                )
                db.add(new_device)
                existing = new_device
                created += 1

            _link_unassigned_inform_device_to_matching_ont(
                db,
                existing,
                serial=serial_number,
                serial_index=serial_index,
            )

            # Flush each batch so a large inventory doesn't hold one long write
            # transaction; the trailing commit below catches the remainder.
            if idx % _GENIEACS_SYNC_COMMIT_BATCH == 0:
                db.commit()

        db.commit()

        synced_ont_status = 0
        try:
            synced_ont_status = _refresh_synced_ont_acs_observations(
                db,
                acs_server_id=str(server.id),
            )
            db.commit()
        except SoftTimeLimitExceeded:
            # Time budget exhausted mid-refresh — don't swallow it as a generic
            # failure; propagate so the task stops cleanly instead of reusing a
            # possibly poisoned connection.
            _safe_rollback(db)
            raise
        except Exception as e:
            logger.warning("Bulk ONT ACS status refresh after sync failed: %s", e)
            db.rollback()

        # Auto-link to ONTs by serial number
        auto_linked = 0
        explicit_links = 0
        serial_updated = 0
        status_snapshot_updated = 0
        try:
            unlinked_devices = (
                db.query(Tr069CpeDevice)
                .filter(
                    Tr069CpeDevice.acs_server_id == acs_server_id,
                    Tr069CpeDevice.is_active.is_(True),
                )
                .all()
            )
            for link_idx, cpe_dev in enumerate(unlinked_devices, start=1):
                # Commit periodically so a large fleet doesn't hold one long
                # write transaction and so a soft-time-limit interrupt keeps the
                # links already made.
                if link_idx % _GENIEACS_AUTOLINK_COMMIT_BATCH == 0:
                    db.commit()
                if not cpe_dev.serial_number:
                    continue
                cpe_serial = str(cpe_dev.serial_number).strip()
                ont = serial_index.find_unique(cpe_serial)

                if not ont and cpe_dev.ont_unit_id:
                    ont = serial_index.get(cpe_dev.ont_unit_id)

                if ont:
                    previous_ont_id = cpe_dev.ont_unit_id
                    link_tr069_device_to_ont(
                        db,
                        cpe_dev,
                        ont,
                        acs_server_id=server.id,
                    )
                    if previous_ont_id != ont.id:
                        explicit_links += 1
                    if cpe_dev.ont_unit_id == ont.id:
                        auto_linked += 1
                    # Update synthetic serials with real GenieACS serial
                    current = str(ont.serial_number or "")
                    if current.startswith("HW-") and cpe_serial != current:
                        ont.serial_number = cpe_serial[:120]
                        serial_updated += 1
                    apply_status_snapshot(
                        ont,
                        resolve_ont_status_snapshot(
                            olt_status=getattr(ont, "olt_status", None),
                            acs_last_inform_at=cpe_dev.last_inform_at,
                            online_window_minutes=(
                                resolve_acs_online_window_minutes_for_model(ont)
                            ),
                        ),
                    )
                    status_snapshot_updated += 1

            if (
                auto_linked
                or explicit_links
                or serial_updated
                or status_snapshot_updated
            ):
                db.commit()
                logger.info(
                    "Auto-link: %d ONTs linked to ACS %s, %d explicit TR-069 links, %d serials updated, %d status snapshots refreshed",
                    auto_linked,
                    server.name,
                    explicit_links,
                    serial_updated,
                    status_snapshot_updated,
                )
        except SoftTimeLimitExceeded:
            # Time budget exhausted mid-pass — propagate instead of swallowing,
            # so the task aborts cleanly rather than reusing a poisoned session.
            _safe_rollback(db)
            raise
        except Exception as e:
            logger.warning("Auto-link ONTs after sync failed: %s", e)
            db.rollback()

        logger.info(
            "GenieACS sync: created=%d, updated=%d, auto_linked=%d",
            created,
            updated,
            auto_linked,
        )
        return {
            "created": created,
            "updated": updated,
            "total": len(devices),
            "auto_linked": auto_linked,
            "synced_ont_status": synced_ont_status,
            "local_onts_checked": 0,
            "local_created": 0,
            "local_reactivated": 0,
            "local_linked": 0,
        }


class Sessions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069SessionCreate):
        session = Tr069Session(**payload.model_dump())
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Session)
        if device_id:
            query = query.filter(Tr069Session.device_id == device_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Tr069Session.created_at,
                "started_at": Tr069Session.started_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, session_id: str, payload: Tr069SessionUpdate):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(session, key, value)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        db.delete(session)
        db.commit()


class Parameters(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069ParameterCreate):
        parameter = Tr069Parameter(**payload.model_dump())
        db.add(parameter)
        db.commit()
        db.refresh(parameter)
        return parameter

    @staticmethod
    def get(db: Session, parameter_id: str):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        return parameter

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Parameter)
        if device_id:
            query = query.filter(Tr069Parameter.device_id == device_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "name": Tr069Parameter.name,
                "updated_at": Tr069Parameter.updated_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, parameter_id: str, payload: Tr069ParameterUpdate):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(parameter, key, value)
        db.commit()
        db.refresh(parameter)
        return parameter

    @staticmethod
    def delete(db: Session, parameter_id: str):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        db.delete(parameter)
        db.commit()


class JobProjections(ListResponseMixin):
    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="TR-069 job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Job)
        if device_id:
            query = query.filter(Tr069Job.device_id == device_id)
        if status:
            query = query.filter(
                Tr069Job.status == validate_enum(status, Tr069JobStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Tr069Job.created_at, "status": Tr069Job.status},
        )
        return apply_pagination(query, limit, offset).all()


def receive_inform(
    db: Session,
    *,
    serial_number: str | None,
    device_id_raw: str | None,
    event: Any,
    raw_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    remote_addr: str | None = None,
    headers: dict[str, Any] | None = None,
    oui: str | None = None,
    product_class: str | None = None,
    acs_server_id: str | None = None,
) -> dict:
    """Process a GenieACS inform webhook callback.

    Resolves or creates the local CPE device, updates last_inform_at, stores
    the full inform payload on a session, and upserts any reported parameters.
    """
    from app.models.tr069 import Tr069Event

    payload = dict(raw_payload or {})
    serial = _first_text(
        serial_number,
        _payload_lookup(payload, "serial", "serialNumber", "serial_number"),
        max_len=120,
    )
    device_id_str = (device_id_raw or "").strip()
    if not device_id_str:
        device_id_str = (
            _first_text(
                _payload_lookup(payload, "device_id", "deviceId", "_id"),
                max_len=255,
            )
            or ""
        )
    if not serial and device_id_str:
        serial = _extract_serial_from_device_id(device_id_str)

    if not serial:
        return {"status": "ignored", "reason": "no serial number"}

    event_candidate = event
    if event_candidate in (None, "", "periodic"):
        event_candidate = (
            _payload_lookup(payload, "event", "events", "Event", "EventList")
            or event_candidate
        )
    event_str, raw_event = _normalize_event_value(event_candidate)

    device = _resolve_device_for_inform(
        db,
        serial=serial,
        device_id_raw=device_id_str,
        acs_server_id=acs_server_id,
        oui=_first_text(oui, _payload_lookup(payload, "oui", "OUI"), max_len=8),
        product_class=_first_text(
            product_class,
            _payload_lookup(payload, "product_class", "productClass", "ProductClass"),
            max_len=120,
        ),
    )
    if not device:
        logger.debug("Inform received for unknown serial: %s", serial)
        return {"status": "ignored", "reason": "unknown device or ACS server"}

    _link_unassigned_inform_device_to_matching_ont(db, device, serial=serial)

    now = datetime.now(UTC)
    previous_last_inform_at = device.last_inform_at
    ont_id_for_service_apply = device.ont_unit_id
    if serial and not device.serial_number:
        device.serial_number = serial[:120]
    if device_id_str:
        _update_genieacs_device_id_from_inform(db, device, device_id_str)
    if oui and not device.oui:
        device.oui = oui[:8]
    if product_class and not device.product_class:
        device.product_class = product_class[:120]
    device.last_inform_at = now
    if device.ont_unit_id:
        from app.models.network import OntUnit

        ont = db.get(OntUnit, device.ont_unit_id)
        if ont:
            apply_status_snapshot(
                ont,
                resolve_ont_status_snapshot(
                    olt_status=getattr(ont, "olt_status", None),
                    acs_last_inform_at=now,
                    online_window_minutes=resolve_acs_online_window_minutes_for_model(
                        ont
                    ),
                ),
            )

    event_map = {
        "boot": Tr069Event.boot,
        "bootstrap": Tr069Event.bootstrap,
        "periodic": Tr069Event.periodic,
        "value_change": Tr069Event.value_change,
        "connection_request": Tr069Event.connection_request,
        "transfer_complete": Tr069Event.transfer_complete,
        "diagnostics_complete": Tr069Event.diagnostics_complete,
    }
    event_type = event_map.get(event_str, Tr069Event.periodic)
    db.flush()
    parameters = _extract_inform_parameters(payload)
    parameter_count = _upsert_inform_parameters(
        db,
        device=device,
        parameters=parameters,
        updated_at=now,
    )

    session = Tr069Session(
        device_id=device.id,
        event_type=event_type,
        request_id=_first_text(request_id, max_len=120),
        started_at=now,
        ended_at=now,
        inform_payload={
            "serial_number": serial,
            "device_id": device_id_str or None,
            "event": event_str,
            "raw_event": _json_safe(raw_event),
            "raw_payload": _json_safe(payload),
            "request": {
                "request_id": request_id,
                "remote_addr": remote_addr,
                "headers": _json_safe(headers or {}),
            },
            "parameter_count": parameter_count,
        },
    )
    db.add(session)
    db.commit()
    commissioning_completed = False
    if ont_id_for_service_apply is not None:
        try:
            from app.services.network.ont_commissioning import (
                complete_commissioning_after_inform,
            )

            commissioning_completed = complete_commissioning_after_inform(
                db,
                ont_id=str(ont_id_for_service_apply),
                reason="tr069_inform",
            )
            if commissioning_completed:
                db.commit()
        except Exception:
            db.rollback()
            logger.warning(
                "Failed to complete commissioning after Inform for ONT %s",
                ont_id_for_service_apply,
                exc_info=True,
            )
    service_apply_queued = False
    try:
        service_apply_queued = _queue_saved_service_apply_after_stale_inform(
            db,
            ont_id=ont_id_for_service_apply,
            previous_last_inform_at=previous_last_inform_at,
            now=now,
        )
    except Exception:
        logger.warning(
            "Failed to queue saved service config apply after inform for ONT %s",
            ont_id_for_service_apply,
            exc_info=True,
        )

    logger.info(
        "Inform received: serial=%s event=%s device_id=%s",
        serial,
        event_str,
        device.id,
    )
    return {
        "status": "ok",
        "device_id": str(device.id),
        "event": event_str,
        "parameters": parameter_count,
        "session_id": str(session.id),
        "service_apply_queued": service_apply_queued,
    }


# -----------------------------------------------------------------------------
# ACS Enforcement Preset Management
# -----------------------------------------------------------------------------

PROVISION_NAME_PREFIX = "dotmac-enforce-acs"
PRESET_NAME_PREFIX = "dotmac-enforce-acs"


def _build_acs_provision_script(
    cwmp_url: str,
    cwmp_username: str | None = None,
    cwmp_password: str | None = None,
    connection_request_username: str | None = None,
    connection_request_password: str | None = None,
    periodic_inform_interval: int = settings.tr069_periodic_inform_interval,
) -> str:
    """Build GenieACS provision script that enforces ACS URL on every inform.

    This provision uses GenieACS's declare() function to set the ManagementServer
    parameters. It handles both TR-181 (Device.*) and TR-098 (InternetGatewayDevice.*)
    data models.

    Args:
        cwmp_url: The ACS CWMP URL to enforce
        cwmp_username: Optional CWMP username
        cwmp_password: Optional CWMP password
        connection_request_username: Optional Connection Request username
        connection_request_password: Optional Connection Request password
        periodic_inform_interval: Inform interval in seconds (default 300)

    Returns:
        JavaScript provision script
    """

    # Escape strings for JavaScript
    def js_string(s: str | None) -> str:
        if s is None:
            return "null"
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    # GenieACS provisions run in a 50 ms VM. If any declare() faults or times out,
    # subsequent top-level statements are abandoned — leaving the device half-configured
    # (notably with empty ConnectionRequestUsername/Password, which then makes every NBI
    # `?connection_request` POST return 202 rather than 200 because GenieACS has no
    # credentials to dial out with). Isolate each declare() in its own try/catch so a
    # single bad path can't take the whole script down.
    def safe_declare(path_js: str, value_js: str) -> str:
        return (
            "try { declare("
            + path_js
            + ", null, {value: "
            + value_js
            + "}); } catch (e) {}"
        )

    script_lines = [
        "// DotMac ACS URL Enforcement Provision",
        "// Automatically generated - do not edit manually",
        f"// Target ACS: {cwmp_url}",
        "",
        "const now = Date.now();",
        "",
        "// Detect data model by checking which root exists",
        'let root = "InternetGatewayDevice";',
        "try {",
        '  const dm = declare("Device.DeviceInfo.Manufacturer", {value: 1});',
        "  if (dm.value && dm.value[0] !== undefined) {",
        '    root = "Device";',
        "  }",
        "} catch (e) {}",
        "",
        "// Set ManagementServer parameters (each isolated so one fault doesn't abort the rest)",
        safe_declare('root + ".ManagementServer.URL"', js_string(cwmp_url)),
        safe_declare('root + ".ManagementServer.PeriodicInformEnable"', "true"),
        safe_declare(
            'root + ".ManagementServer.PeriodicInformInterval"',
            str(int(periodic_inform_interval)),
        ),
    ]

    if cwmp_username:
        script_lines.append(
            safe_declare(
                'root + ".ManagementServer.Username"', js_string(cwmp_username)
            )
        )

    if cwmp_password:
        script_lines.append(
            safe_declare(
                'root + ".ManagementServer.Password"', js_string(cwmp_password)
            )
        )

    if connection_request_username:
        script_lines.append(
            safe_declare(
                'root + ".ManagementServer.ConnectionRequestUsername"',
                js_string(connection_request_username),
            )
        )

    if connection_request_password:
        script_lines.append(
            safe_declare(
                'root + ".ManagementServer.ConnectionRequestPassword"',
                js_string(connection_request_password),
            )
        )

    return "\n".join(script_lines)


def _build_acs_preset(
    preset_id: str,
    provision_name: str,
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = False,
    precondition: str = "",
    weight: int = 100,
) -> dict:
    """Build GenieACS preset definition.

    Args:
        preset_id: Unique preset ID
        provision_name: Name of the provision script to run
        on_bootstrap: Run on bootstrap event (device first contact)
        on_boot: Run on boot event
        on_periodic: Run on periodic inform
        precondition: Optional MongoDB-style filter to limit which devices
        weight: Preset priority (higher = runs later, default 100)

    Returns:
        Preset definition dict
    """
    events = {}
    if on_bootstrap:
        events["0 BOOTSTRAP"] = True
    if on_boot:
        events["1 BOOT"] = True
    if on_periodic:
        events["2 PERIODIC"] = True

    return {
        "_id": preset_id,
        "weight": weight,
        "events": events,
        "precondition": precondition,
        "configurations": [{"type": "provision", "name": provision_name, "args": []}],
    }


def push_acs_enforcement_preset(
    db: Session,
    acs_server_id: str,
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = False,
    precondition: str = "",
) -> dict:
    """Push ACS enforcement provision and preset to GenieACS.

    Creates a provision script that sets ManagementServer.URL to this ACS server's
    CWMP URL, and a preset that runs it on specified events. By default this is
    limited to bootstrap/boot so periodic informs remain a lightweight heartbeat.

    Args:
        db: Database session
        acs_server_id: The ACS server to enforce
        on_bootstrap: Run on device bootstrap (first contact)
        on_boot: Run on device boot
        on_periodic: Run on periodic inform
        precondition: MongoDB-style filter to limit affected devices

    Returns:
        Dict with provision_id, preset_id, and status
    """
    from app.services.credential_crypto import decrypt_credential

    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.cwmp_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no CWMP URL configured"
        )

    if not server.base_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no GenieACS base URL configured"
        )

    # Build unique IDs based on server
    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    # Decrypt passwords if set
    cwmp_password = None
    if server.cwmp_password:
        cwmp_password = decrypt_credential(server.cwmp_password)
    connection_request_password = None
    if server.connection_request_password:
        connection_request_password = decrypt_credential(
            server.connection_request_password
        )

    # Build provision script using server's configured interval
    provision_script = _build_acs_provision_script(
        cwmp_url=server.cwmp_url,
        cwmp_username=server.cwmp_username,
        cwmp_password=cwmp_password,
        connection_request_username=server.connection_request_username,
        connection_request_password=connection_request_password,
        periodic_inform_interval=server.periodic_inform_interval
        or settings.tr069_periodic_inform_interval,
    )

    # Build preset
    preset = _build_acs_preset(
        preset_id=preset_id,
        provision_name=provision_name,
        on_bootstrap=on_bootstrap,
        on_boot=on_boot,
        on_periodic=on_periodic,
        precondition=precondition,
        weight=100,  # High weight to run after other presets
    )

    # Push to GenieACS
    client = create_genieacs_client(server.base_url)

    try:
        # Create provision first
        client.create_provision(provision_name, provision_script)
        logger.info("Created ACS enforcement provision: %s", provision_name)
    except GenieACSError as exc:
        logger.error("Failed to create provision %s: %s", provision_name, exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to create provision: {exc}"
        ) from exc

    try:
        # Create preset
        client.create_preset(preset)
        logger.info("Created ACS enforcement preset: %s", preset_id)
    except GenieACSError as exc:
        logger.error("Failed to create preset %s: %s", preset_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to create preset: {exc}"
        ) from exc

    return {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "cwmp_url": server.cwmp_url,
        "events": {
            "bootstrap": on_bootstrap,
            "boot": on_boot,
            "periodic": on_periodic,
        },
        "status": "created",
    }


def remove_acs_enforcement_preset(db: Session, acs_server_id: str) -> dict:
    """Remove ACS enforcement provision and preset from GenieACS.

    Args:
        db: Database session
        acs_server_id: The ACS server whose enforcement to remove

    Returns:
        Dict with removal status
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no GenieACS base URL configured"
        )

    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    client = create_genieacs_client(server.base_url)
    removed = {"provision": False, "preset": False}

    try:
        client.delete_preset(preset_id)
        removed["preset"] = True
        logger.info("Removed ACS enforcement preset: %s", preset_id)
    except GenieACSError as exc:
        logger.warning("Failed to remove preset %s: %s", preset_id, exc)

    try:
        client.delete_provision(provision_name)
        removed["provision"] = True
        logger.info("Removed ACS enforcement provision: %s", provision_name)
    except GenieACSError as exc:
        logger.warning("Failed to remove provision %s: %s", provision_name, exc)

    return {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "removed": removed,
        "status": "removed" if any(removed.values()) else "not_found",
    }


def get_acs_enforcement_status(db: Session, acs_server_id: str) -> dict:
    """Check if ACS enforcement preset exists in GenieACS.

    Args:
        db: Database session
        acs_server_id: The ACS server to check

    Returns:
        Dict with existence status and details
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        return {
            "exists": False,
            "error": "ACS server has no GenieACS base URL configured",
        }

    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    client = create_genieacs_client(server.base_url)
    status = {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "provision_exists": False,
        "preset_exists": False,
        "preset_details": None,
    }

    try:
        provisions = client.list_provisions()
        status["provision_exists"] = any(
            p.get("_id") == provision_name for p in provisions
        )
    except GenieACSError as exc:
        logger.warning("Failed to list provisions: %s", exc)

    try:
        presets = client.list_presets()
        for preset in presets:
            if preset.get("_id") == preset_id:
                status["preset_exists"] = True
                status["preset_details"] = {
                    "events": preset.get("events", {}),
                    "precondition": preset.get("precondition", ""),
                    "weight": preset.get("weight", 0),
                }
                break
    except GenieACSError as exc:
        logger.warning("Failed to list presets: %s", exc)

    status["exists"] = status["provision_exists"] and status["preset_exists"]
    return status


acs_servers = AcsServers()
cpe_devices = CpeDevices()
sessions = Sessions()
parameters = Parameters()
jobs = JobProjections()
