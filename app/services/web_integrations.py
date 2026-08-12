"""Service helpers for admin integrations web routes."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.integration import (
    IntegrationJob,
    IntegrationRun,
    IntegrationRunStatus,
)
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import operational_checks as operational_checks_service
from app.services.common import validate_enum
from app.services.integrations import installations
from app.services.integrations import registry as integration_registry
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)
from app.validators.forms import parse_uuid

logger = logging.getLogger(__name__)


def _parse_json(value: str | None, field: str) -> dict | None:
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def connector_form_options() -> dict[str, object]:
    from app.models.connector import ConnectorAuthType, ConnectorType

    return {
        "connector_types": [t.value for t in ConnectorType],
        "auth_types": [t.value for t in ConnectorAuthType],
    }


def connector_error_state(
    *,
    name: str,
    connector_type: str,
    auth_type: str,
    base_url: str | None,
    timeout_sec: str | None,
    auth_config: str | None,
    headers: str | None,
    retry_policy: str | None,
    metadata: str | None,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **connector_form_options(),
        "form": {
            "name": name,
            "connector_type": connector_type,
            "auth_type": auth_type,
            "base_url": base_url or "",
            "timeout_sec": timeout_sec or "",
            "auth_config": auth_config or "",
            "headers": headers or "",
            "retry_policy": retry_policy or "",
            "metadata": metadata or "",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def target_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationTargetType

    return {"target_types": [t.value for t in IntegrationTargetType]}


def target_error_state(
    db,
    *,
    name: str,
    target_type: str,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **target_form_options(db),
        "form": {
            "name": name,
            "target_type": target_type,
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def job_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationJobType, IntegrationScheduleType
    from app.models.integration_platform import (
        IntegrationBindingState,
        IntegrationCapabilityBinding,
        IntegrationInstallation,
        IntegrationInstallationState,
    )

    targets = integration_service.integration_targets.list(
        db=db,
        target_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    bindings = (
        db.query(IntegrationCapabilityBinding)
        .join(IntegrationInstallation)
        .filter(
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value,
        )
        .order_by(
            IntegrationInstallation.name.asc(),
            IntegrationCapabilityBinding.capability_id.asc(),
        )
        .all()
    )
    return {
        "job_types": [t.value for t in IntegrationJobType],
        "schedule_types": [t.value for t in IntegrationScheduleType],
        "directions": ["pull", "push", "bidirectional"],
        "trigger_modes": ["manual", "schedule", "event", "webhook"],
        "conflict_policies": [
            "remote_wins",
            "local_wins",
            "newest_wins",
            "manual_review",
        ],
        "targets": targets,
        "capability_bindings": bindings,
    }


def job_error_state(
    db,
    *,
    target_id: str,
    name: str,
    job_type: str,
    schedule_type: str,
    interval_minutes: str | None,
    capability_binding_id: str | None = None,
    entity_type: str | None = None,
    direction: str | None = None,
    trigger_mode: str | None = None,
    mapping_config: str | None = None,
    filter_config: str | None = None,
    conflict_policy: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
) -> dict[str, object]:
    return {
        **job_form_options(db),
        "form": {
            "target_id": target_id,
            "name": name,
            "job_type": job_type,
            "schedule_type": schedule_type,
            "interval_minutes": interval_minutes or "",
            "capability_binding_id": capability_binding_id or "",
            "entity_type": entity_type or "",
            "direction": direction or "pull",
            "trigger_mode": trigger_mode or "manual",
            "mapping_config": mapping_config or "",
            "filter_config": filter_config or "",
            "conflict_policy": conflict_policy or "remote_wins",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def connector_stats(connectors: list) -> dict[str, object]:
    by_type: dict[str, int] = {}
    stats = {
        "total": len(connectors),
        "active": sum(1 for c in connectors if c.is_active),
        "by_type": by_type,
    }
    for connector in connectors:
        ctype = (
            connector.connector_type.value
            if hasattr(connector.connector_type, "value")
            else str(connector.connector_type or "custom")
        )
        by_type[ctype] = by_type.get(ctype, 0) + 1
    return stats


def build_connectors_list_data(db) -> dict[str, object]:
    connectors = connector_service.connector_configs.list_all(
        db=db,
        connector_type=None,
        auth_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    installations_rows = [
        row
        for row in installations.list_installations(db, limit=500)
        if row.state != IntegrationInstallationState.retired.value
    ]
    managed_connectors = [
        {
            "installation": installation,
            "definition": integration_registry.require_connector_definition(
                installation.connector_key
            ),
            "manage_url": _installation_manage_url(installation),
        }
        for installation in installations_rows
    ]
    stats = connector_stats(connectors)
    stats["total"] += len(managed_connectors)
    stats["active"] += sum(
        row.state == IntegrationInstallationState.enabled.value
        for row in installations_rows
    )
    return {
        "connectors": connectors,
        "managed_connectors": managed_connectors,
        "stats": stats,
    }


def _parse_version_tuple(version: str) -> tuple[int, int, int]:
    raw = (version or "0.0.0").strip().lower().lstrip("v")
    parts = raw.split(".")
    nums = []
    for item in parts[:3]:
        digits = "".join(ch for ch in item if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def build_marketplace_data(db) -> dict[str, object]:
    installed_rows = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.state != IntegrationInstallationState.retired.value
        )
        .order_by(IntegrationInstallation.created_at.asc())
        .all()
    )
    installed_by_key = {
        installation.connector_key: installation for installation in installed_rows
    }
    discovered = integration_registry.discover_connectors()
    cards: list[dict[str, object]] = []
    for entry in discovered:
        installed = installed_by_key.get(entry.key)
        definition = integration_registry.require_connector_definition(entry.key)
        installed_version = installed.connector_version if installed else None
        update_available = bool(
            installed
            and _parse_version_tuple(entry.version)
            > _parse_version_tuple(installed_version or "0.0.0")
        )
        cards.append(
            {
                "key": entry.key,
                "name": entry.name,
                "description": entry.description,
                "type": entry.connector_type,
                "available_version": entry.version,
                "module_name": entry.module_name,
                "file_size_bytes": entry.file_size_bytes,
                "installed": bool(installed),
                "installed_installation": installed,
                "installed_version": installed_version,
                "update_available": update_available,
                "last_sync": installed.updated_at if installed else None,
                "manage_url": _installation_manage_url(installed)
                if installed
                else None,
                "install_url": (
                    "/admin/integrations/whatsapp"
                    if entry.key == "whatsapp"
                    else (
                        "/admin/crm/meta"
                        if entry.key == "meta.social"
                        else (
                            f"/admin/integrations/payment-gateways/{entry.key}"
                            if entry.key in {"paystack", "flutterwave"}
                            else None
                        )
                    )
                ),
                "installable": (definition.runtime.type.value != "catalogue_only"),
            }
        )

    return {
        "marketplace_cards": cards,
        "stats": {
            "available": len(cards),
            "installed": sum(1 for card in cards if card["installed"]),
            "updates": sum(1 for card in cards if card["update_available"]),
        },
    }


def _installation_manage_url(installation: IntegrationInstallation) -> str:
    if installation.connector_key == "dotmac.erp":
        return "/admin/integrations/erp"
    if installation.connector_key == "webhook.http":
        return f"/admin/integrations/webhooks/{installation.id}"
    if installation.connector_key == "whatsapp":
        return "/admin/integrations/whatsapp"
    if installation.connector_key == "meta.social":
        return "/admin/crm/meta"
    if installation.connector_key in {"paystack", "flutterwave"}:
        return f"/admin/integrations/payment-gateways/{installation.connector_key}"
    return "/admin/integrations/jobs"


def _latest_run_by_installation(
    db: Session, installation_ids: list[UUID]
) -> dict[str, tuple[str, datetime | None]]:
    """Most recent completed capability run per installation."""
    if not installation_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=IntegrationRun.installation_id,
            order_by=IntegrationRun.started_at.desc(),
        )
        .label("rank")
    )
    ranked = (
        db.query(
            IntegrationRun.installation_id.label("installation_id"),
            IntegrationRun.status.label("status"),
            IntegrationRun.started_at.label("started_at"),
            rank,
        )
        .filter(IntegrationRun.installation_id.in_(installation_ids))
        .filter(IntegrationRun.status != IntegrationRunStatus.running)
        .subquery()
    )
    rows = (
        db.query(ranked.c.installation_id, ranked.c.status, ranked.c.started_at)
        .filter(ranked.c.rank == 1)
        .all()
    )
    return {
        str(row.installation_id): (
            getattr(row.status, "value", str(row.status)),
            row.started_at,
        )
        for row in rows
    }


def _delivery_stats_by_installation(
    db: Session, installation_ids: list[UUID]
) -> dict[str, tuple[int, int]]:
    if not installation_ids:
        return {}
    rows = (
        db.query(
            IntegrationCapabilityBinding.installation_id,
            func.count(IntegrationDelivery.id).label("calls"),
            func.sum(
                case(
                    (
                        IntegrationDelivery.state.in_(
                            ("dead_letter", "reconciliation_required")
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("failed"),
        )
        .join(
            IntegrationDelivery,
            IntegrationDelivery.capability_binding_id
            == IntegrationCapabilityBinding.id,
        )
        .filter(IntegrationCapabilityBinding.installation_id.in_(installation_ids))
        .group_by(IntegrationCapabilityBinding.installation_id)
        .all()
    )
    return {
        str(row.installation_id): (int(row.calls or 0), int(row.failed or 0))
        for row in rows
    }


def _installation_evidence(
    last_run: tuple[str, datetime | None] | None,
    delivery_stats: tuple[int, int] | None,
) -> dict[str, object]:
    """Explain runtime evidence without collapsing it into a health label."""
    calls, failed = delivery_stats or (0, 0)
    last_status = last_run[0] if last_run else None
    if last_status == "success":
        result = "The last completed integration job succeeded."
    elif last_status == "failed":
        result = "The last completed integration job failed."
    elif calls:
        result = f"{calls} delivery attempt(s) recorded; {failed} require attention."
    else:
        result = "No completed job or delivery evidence has been recorded."
    return {
        "last_result": result,
        "observed_at": last_run[1] if last_run else None,
        "calls": calls,
        "failed": failed,
        "last_run_status": last_status,
        "needs_attention": bool(last_status == "failed" or failed),
    }


def build_installed_integrations_data(db: Session) -> dict[str, object]:
    paystack_operational_check = operational_checks_service.paystack_payment_check(
        db
    ).to_dict()
    crm_operational_check = operational_checks_service.crm_operational_check(
        db
    ).to_dict()
    installed = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.state != IntegrationInstallationState.retired.value
        )
        .order_by(
            IntegrationInstallation.connector_key.asc(),
            IntegrationInstallation.name.asc(),
        )
        .all()
    )
    installation_ids = [installation.id for installation in installed]
    installation_names = {
        str(installation.id): installation.name for installation in installed
    }
    last_runs = _latest_run_by_installation(db, installation_ids)
    delivery_stats = _delivery_stats_by_installation(db, installation_ids)
    rows: list[dict[str, object]] = []
    for installation in installed:
        definition = integration_registry.require_connector_definition(
            installation.connector_key
        )
        evidence = _installation_evidence(
            last_runs.get(str(installation.id)),
            delivery_stats.get(str(installation.id)),
        )
        rows.append(
            {
                "installation": installation,
                "title": definition.name,
                "root": "integrations",
                "integration_type": definition.connector_type,
                "operational_evidence": evidence,
                "manage_url": _installation_manage_url(installation),
            }
        )

    activities: list[dict[str, object]] = []
    if installation_ids:
        run_rows = (
            db.query(
                IntegrationRun,
                IntegrationJob.name.label("job_name"),
            )
            .join(IntegrationJob, IntegrationRun.job_id == IntegrationJob.id)
            .filter(IntegrationRun.installation_id.in_(installation_ids))
            .order_by(IntegrationRun.started_at.desc())
            .limit(50)
            .all()
        )
        for run, job_name in run_rows:
            duration_ms = None
            if run.finished_at is not None and run.started_at is not None:
                duration_ms = int(
                    (run.finished_at - run.started_at).total_seconds() * 1000
                )
            installation_key = str(run.installation_id)
            activities.append(
                {
                    "installation_id": installation_key,
                    "connector_id": installation_key,
                    "connector_name": installation_names.get(
                        installation_key, installation_key
                    ),
                    "timestamp": run.started_at,
                    "event_type": f"job: {job_name}",
                    "status_code": None,
                    "response_time_ms": duration_ms,
                    "status": getattr(run.status, "value", str(run.status)),
                }
            )
        delivery_rows = (
            db.query(IntegrationDelivery, IntegrationCapabilityBinding.installation_id)
            .join(
                IntegrationCapabilityBinding,
                IntegrationDelivery.capability_binding_id
                == IntegrationCapabilityBinding.id,
            )
            .filter(IntegrationCapabilityBinding.installation_id.in_(installation_ids))
            .order_by(IntegrationDelivery.created_at.desc())
            .limit(50)
            .all()
        )
        for delivery_row, delivery_installation_id in delivery_rows:
            installation_key = str(delivery_installation_id)
            activities.append(
                {
                    "installation_id": installation_key,
                    "connector_id": installation_key,
                    "connector_name": installation_names.get(
                        installation_key, installation_key
                    ),
                    "timestamp": delivery_row.last_attempt_at
                    or delivery_row.created_at,
                    "event_type": f"event: {delivery_row.event_type}",
                    "status_code": delivery_row.response_status,
                    "response_time_ms": None,
                    "status": delivery_row.state,
                }
            )
    activities.sort(key=lambda item: item["timestamp"], reverse=True)
    activities = activities[:50]
    return {
        "integrations": rows,
        "activity_log": activities,
        "stats": {
            "total": len(rows),
            "enabled": sum(
                1
                for installation in installed
                if installation.state == IntegrationInstallationState.enabled.value
            ),
            "attention": sum(
                1
                for row in rows
                if bool(
                    cast(
                        dict[str, object],
                        row["operational_evidence"],
                    ).get("needs_attention")
                )
            ),
        },
        "integration_operational_checks": [
            paystack_operational_check,
            crm_operational_check,
        ],
        "paystack_operational_check": paystack_operational_check,
        "crm_operational_check": crm_operational_check,
    }


def bulk_set_integrations_enabled(
    db: Session, connector_ids: list[str], *, enabled: bool
) -> int:
    changed: list[IntegrationInstallation] = []
    for installation_id in connector_ids:
        installation = installations.get_installation(db, installation_id)
        if enabled:
            static_result = installations.validate_static(
                db,
                installation_id=installation.id,
                actor="admin.integrations.installed",
            )
            if not static_result.valid:
                raise ValueError(
                    "Static validation failed: " + ", ".join(static_result.error_codes)
                )
            if not installation.capability_bindings:
                raise ValueError("Installation has no capability bindings")
            context = build_execution_context(
                db,
                capability_binding_id=installation.capability_bindings[0].id,
                allow_disabled=True,
            )
            installations.enable_after_connection_validation(
                db,
                installation_id=installation.id,
                connection_result=validate_connection(context),
                actor="admin.integrations.installed",
            )
        else:
            installations.disable_installation(
                db,
                installation_id=installation.id,
                reason="operator_disabled",
                actor="admin.integrations.installed",
            )
        changed.append(installation)
    if changed:
        installations.commit_installation_changes(db, changed[-1])
    return len(changed)


def uninstall_integration(db: Session, connector_id: str):
    installation = installations.retire_installation(
        db,
        installation_id=connector_id,
        reason="operator_retired",
        actor="admin.integrations.installed",
    )
    return installations.commit_installation_changes(db, installation)


def create_connector(
    db,
    *,
    name: str,
    connector_type: str,
    auth_type: str,
    base_url: str | None,
    timeout_sec: str | None,
    auth_config: str | None,
    headers: str | None,
    retry_policy: str | None,
    metadata: str | None,
    notes: str | None = None,
    is_active: bool = True,
):
    from app.models.connector import ConnectorAuthType, ConnectorType

    payload = ConnectorConfigCreate(
        name=name.strip(),
        connector_type=validate_enum(connector_type, ConnectorType, "connector_type"),
        auth_type=validate_enum(auth_type, ConnectorAuthType, "auth_type"),
        base_url=base_url.strip() if base_url else None,
        timeout_sec=int(timeout_sec) if timeout_sec else None,
        auth_config=_parse_json(auth_config, "auth_config"),
        headers=_parse_json(headers, "headers"),
        retry_policy=_parse_json(retry_policy, "retry_policy"),
        metadata_=_parse_json(metadata, "metadata"),
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return connector_service.connector_configs.create(db, payload)


# Header/metadata keys whose values are secrets — masked on display so a connector
# detail page never re-renders pasted tokens (e.g. an Authorization header).
SECRET_VALUE_SENTINEL = "__redacted__"
_SECRET_KEY_HINTS = (
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "api_key",
    "apikey",
    "api-key",
    "auth",
    "credential",
)


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def mask_secret_values(data: dict | None) -> dict:
    """Copy of ``data`` with secret-keyed values replaced by a sentinel."""
    if not isinstance(data, dict):
        return {}
    return {
        key: (SECRET_VALUE_SENTINEL if (_is_secret_key(key) and value) else value)
        for key, value in data.items()
    }


def _unmask_secret_values(submitted: object, stored: dict | None) -> object:
    """Restore sentinel values in a submitted dict from the stored original.

    Lets the edit form show masked secrets and keep them on save unless the
    operator types a new value over the mask.
    """
    if not isinstance(submitted, dict):
        return submitted
    stored = stored if isinstance(stored, dict) else {}
    return {
        key: (stored.get(key) if value == SECRET_VALUE_SENTINEL else value)
        for key, value in submitted.items()
    }


def update_connector_config(
    db,
    connector_id: str,
    *,
    base_url: str | None,
    auth_type: str,
    timeout_sec: str | None,
    auth_config: str | None,
    headers: str | None,
    retry_policy: str | None,
    metadata: str | None,
    notes: str | None,
    is_active: bool,
):
    from app.models.connector import ConnectorAuthType

    existing = connector_service.connector_configs.get(db, connector_id)
    timeout_value = int(timeout_sec) if timeout_sec else None
    payload = ConnectorConfigUpdate(
        base_url=base_url.strip() if base_url else None,
        auth_type=validate_enum(auth_type, ConnectorAuthType, "auth_type"),
        timeout_sec=timeout_value,
        auth_config=_parse_json(auth_config, "auth_config"),
        headers=_unmask_secret_values(
            _parse_json(headers, "headers"), existing.headers
        ),
        retry_policy=_parse_json(retry_policy, "retry_policy"),
        metadata_=_unmask_secret_values(
            _parse_json(metadata, "metadata"), existing.metadata_
        ),
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return connector_service.connector_configs.update(db, connector_id, payload)


def build_embedded_connector_data(
    db,
    *,
    connector_id: str,
    perform_check: bool = False,
) -> dict[str, object]:
    connector = connector_service.connector_configs.get(db, connector_id)
    base_url = (connector.base_url or "").strip()
    parsed = urlparse(base_url) if base_url else None
    is_http = bool(parsed and parsed.scheme in {"http", "https"} and parsed.netloc)
    health_status = "ready" if is_http else "misconfigured"
    health_http_status: int | None = None
    probe_checked = False
    if perform_check and is_http:
        probe_checked = True
        health_status, health_http_status, health_message = _probe_embedded_url_health(
            base_url
        )
    else:
        health_message = (
            "Connector URL is not configured or invalid. Set a full http(s) base URL to embed this integration."
            if not is_http
            else "Connection appears configured. Use 'Check Connection' to probe reachability."
        )
    return {
        "connector": connector,
        "embed_url": base_url if is_http else "",
        "health_status": health_status,
        "health_http_status": health_http_status,
        "probe_checked": probe_checked,
        "health_message": health_message,
    }


def _probe_embedded_url_health(url: str) -> tuple[str, int | None, str]:
    try:
        response = httpx.get(url, timeout=6.0, follow_redirects=True)
    except Exception as exc:
        return (
            "unreachable",
            None,
            f"Connection check failed: {exc}. Confirm DNS/network reachability and remote service availability.",
        )

    status = int(response.status_code)
    if 200 <= status < 300:
        return (
            "ready",
            status,
            f"Connection check succeeded ({status}). If iframe still fails, target may block embedding with X-Frame-Options/CSP.",
        )
    if status in {401, 403}:
        return (
            "auth_required",
            status,
            f"Service is reachable but denied access ({status}). Configure credentials/session and verify embed permissions.",
        )
    if status >= 500:
        return (
            "unreachable",
            status,
            f"Service returned server error ({status}). Check upstream service health and logs.",
        )
    return (
        "degraded",
        status,
        f"Service responded with status {status}. Verify endpoint path and access controls.",
    )


def target_stats(targets: list) -> dict[str, object]:
    by_type: dict[str, int] = {}
    stats = {
        "total": len(targets),
        "active": sum(1 for t in targets if t.is_active),
        "by_type": by_type,
    }
    for target in targets:
        ttype = (
            target.target_type.value
            if hasattr(target.target_type, "value")
            else str(target.target_type or "custom")
        )
        by_type[ttype] = by_type.get(ttype, 0) + 1
    return stats


def build_targets_list_data(db) -> dict[str, object]:
    targets = integration_service.integration_targets.list_all(
        db=db,
        target_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "targets": targets,
        "stats": target_stats(targets),
    }


def create_target(
    db,
    *,
    name: str,
    target_type: str,
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationTargetType

    payload = IntegrationTargetCreate(
        name=name.strip(),
        target_type=validate_enum(target_type, IntegrationTargetType, "target_type"),
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return integration_service.integration_targets.create(db, payload)


def job_stats(jobs: list) -> dict[str, int]:
    def _schedule_value(item):
        schedule = getattr(item, "schedule_type", None)
        if hasattr(schedule, "value"):
            return schedule.value
        return str(schedule) if schedule else None

    return {
        "total": len(jobs),
        "active": sum(1 for j in jobs if j.is_active),
        "manual": sum(1 for j in jobs if _schedule_value(j) == "manual"),
        "scheduled": sum(1 for j in jobs if _schedule_value(j) == "interval"),
    }


def build_jobs_list_data(db) -> dict[str, object]:
    jobs = integration_service.integration_jobs.list_all(
        db=db,
        target_id=None,
        job_type=None,
        schedule_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    job_runs = {}
    for job in jobs:
        recent_runs = integration_service.integration_runs.list(
            db=db,
            job_id=str(job.id),
            status=None,
            order_by="started_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )
        job_runs[str(job.id)] = recent_runs
    return {
        "jobs": jobs,
        "job_runs": job_runs,
        "stats": job_stats(jobs),
    }


def create_job(
    db,
    *,
    target_id: str,
    name: str,
    job_type: str,
    schedule_type: str,
    interval_minutes: str | None,
    capability_binding_id: str,
    entity_type: str | None = None,
    direction: str | None = None,
    trigger_mode: str | None = None,
    mapping_config: str | None = None,
    filter_config: str | None = None,
    conflict_policy: str | None = None,
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationJobType, IntegrationScheduleType

    interval_value = int(interval_minutes) if interval_minutes else None
    if schedule_type == "interval" and not interval_value:
        raise ValueError("interval_minutes is required for interval schedules")
    payload = IntegrationJobCreate(
        target_id=cast(UUID, parse_uuid(target_id, "target_id")),
        capability_binding_id=cast(
            UUID, parse_uuid(capability_binding_id, "capability_binding_id")
        ),
        name=name.strip(),
        job_type=validate_enum(job_type, IntegrationJobType, "job_type"),
        schedule_type=validate_enum(
            schedule_type, IntegrationScheduleType, "schedule_type"
        ),
        interval_minutes=interval_value,
        entity_type=(entity_type or "").strip() or None,
        direction=(direction or "").strip() or None,
        trigger_mode=(trigger_mode or "").strip() or None,
        mapping_config=_parse_json(mapping_config, "mapping_config"),
        filter_config=_parse_json(filter_config, "filter_config"),
        conflict_policy=(conflict_policy or "").strip() or None,
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return integration_service.integration_jobs.create(db, payload)
