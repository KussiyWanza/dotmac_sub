"""Read-only effective-state projection for the admin control plane.

Each row explains a decision made by an existing owning service.  This module
does not write settings or duplicate policy; it presents effective value,
provenance, precedence, scope, health, last change, and nearby audit evidence.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationEventSubscription,
    IntegrationInstallation,
)
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserRole,
)
from app.models.scheduler import ScheduledTask, ScheduleType
from app.models.subscription_engine import SettingValueType
from app.services import control_registry, settings_spec
from app.services.audit_helpers import (
    humanize_action,
    resolve_actor_display_names,
)
from app.services.redis_client import redis_health_check
from app.services.web_integrations import build_installed_integrations_data
from app.services.web_system_secrets import build_secrets_index_context

logger = logging.getLogger(__name__)

_AUDIT_TERMS: dict[str, tuple[str, ...]] = {
    "settings": ("setting", "config", "module", "control"),
    "rbac": ("rbac", "role", "permission"),
    "sessions": ("session", "auth", "login", "logout"),
    "scheduler": ("scheduler", "scheduled_task", "task"),
    "secrets": ("secret", "openbao", "vault"),
    "integrations": ("integration", "connector"),
    "webhooks": ("webhook",),
}


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _display_value(value: object, *, secret: bool = False) -> str:
    if secret:
        return "Configured" if value not in (None, "", {}) else "Missing"
    if value is None:
        return "Not configured"
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        rendered = str(value)
    return rendered if len(rendered) <= 240 else f"{rendered[:237]}…"


def _entry(
    *,
    key: str,
    label: str,
    value: object,
    source: str,
    precedence: str,
    scope: str,
    health: str,
    last_change: object | None,
    detail_url: str | None = None,
    secret: bool = False,
) -> dict[str, object]:
    return {
        "key": key,
        "label": label,
        "effective_value": _display_value(value, secret=secret),
        "source": source,
        "precedence": precedence,
        "scope": scope,
        "health": health,
        "last_change": last_change,
        "detail_url": detail_url,
    }


def _effective_setting(
    spec, setting: DomainSetting | None
) -> tuple[object, str | None]:
    raw = settings_spec.extract_db_value(setting)
    if raw is None:
        raw = spec.default
    value, error = settings_spec.coerce_value(spec, raw)
    if error:
        return spec.default, error
    if spec.allowed and value is not None and value not in spec.allowed:
        return spec.default, "Value is outside the allowed set"
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = (
            value if isinstance(value, int) and not isinstance(value, bool) else None
        )
        if parsed is None:
            return spec.default, "Value must be an integer"
        if spec.min_value is not None and parsed < spec.min_value:
            return spec.default, f"Value is below minimum {spec.min_value}"
        if spec.max_value is not None and parsed > spec.max_value:
            return spec.default, f"Value is above maximum {spec.max_value}"
    return value, None


def _settings_entries(db: Session) -> list[dict[str, object]]:
    rows = db.query(DomainSetting).filter(DomainSetting.is_active.is_(True)).all()
    by_key = {(row.domain, row.key): row for row in rows}
    entries: list[dict[str, object]] = []

    for control in sorted(control_registry.all_controls(), key=lambda item: item.key):
        resolution = control_registry.resolve_control(db, control.key)
        health = "healthy" if resolution.enabled else "disabled"
        if resolution.module_enabled is False and resolution.own_enabled:
            health = "degraded"
        entries.append(
            _entry(
                key=f"control:{control.key}",
                label=control.description or control.key,
                value=resolution.enabled,
                source=resolution.source,
                precedence=resolution.precedence,
                scope=resolution.affected_scope,
                health=health,
                last_change=resolution.updated_at,
                detail_url="/admin/system/modules",
            )
        )

    for spec in sorted(
        settings_spec.SETTINGS_SPECS, key=lambda item: (item.domain.value, item.key)
    ):
        setting = by_key.get((spec.domain, spec.key))
        value, error = _effective_setting(spec, setting)
        missing_required = spec.required and value in (None, "", {})
        health = "critical" if missing_required else "degraded" if error else "healthy"
        entries.append(
            _entry(
                key=f"{spec.domain.value}.{spec.key}",
                label=spec.label or spec.key.replace("_", " ").title(),
                value=value,
                source=(
                    f"database ({spec.domain.value}.{spec.key})"
                    if setting is not None
                    else "registry default"
                ),
                precedence=(
                    "database row → registry default; environment seeds the "
                    "database at bootstrap"
                    if spec.env_var
                    else "database row → registry default"
                ),
                scope=f"{spec.domain.value} domain",
                health=health,
                last_change=setting.updated_at if setting is not None else None,
                detail_url="/admin/system/settings-hub",
                secret=spec.is_secret,
            )
        )
    return entries


def _rbac_entries(db: Session) -> list[dict[str, object]]:
    permissions = {
        role_id: int(count)
        for role_id, count in db.query(
            RolePermission.role_id, func.count(RolePermission.id).label("count")
        )
        .group_by(RolePermission.role_id)
        .all()
    }
    subscriber_grants = {
        role_id: int(count)
        for role_id, count in db.query(
            SubscriberRole.role_id, func.count(SubscriberRole.id).label("count")
        )
        .group_by(SubscriberRole.role_id)
        .all()
    }
    system_grants = {
        role_id: int(count)
        for role_id, count in db.query(
            SystemUserRole.role_id, func.count(SystemUserRole.id).label("count")
        )
        .group_by(SystemUserRole.role_id)
        .all()
    }
    scopes_by_role: dict[object, set[str]] = {}
    for role_id, scope_type, scope_id in [
        *db.query(
            SubscriberRole.role_id,
            SubscriberRole.scope_type,
            SubscriberRole.scope_id,
        ).all(),
        *db.query(
            SystemUserRole.role_id,
            SystemUserRole.scope_type,
            SystemUserRole.scope_id,
        ).all(),
    ]:
        scope = f"{scope_type}:{scope_id}" if scope_type and scope_id else "global"
        scopes_by_role.setdefault(role_id, set()).add(scope)
    entries = []
    for role in db.query(Role).order_by(Role.name).all():
        permission_count = permissions.get(role.id, 0)
        grant_count = subscriber_grants.get(role.id, 0) + system_grants.get(role.id, 0)
        scopes = sorted(scopes_by_role.get(role.id, set()))
        scope_summary = ", ".join(scopes[:5]) if scopes else "not assigned"
        if len(scopes) > 5:
            scope_summary = f"{scope_summary}, +{len(scopes) - 5} more"
        entries.append(
            _entry(
                key=f"role:{role.name}",
                label=role.name,
                value=(
                    f"{'Active' if role.is_active else 'Disabled'}; "
                    f"{permission_count} permissions; {grant_count} assignments"
                ),
                source="RBAC database",
                precedence=(
                    "wildcard admin/token scopes → active direct grant → active "
                    "role permission → deny"
                ),
                scope=scope_summary,
                health=(
                    "disabled"
                    if not role.is_active
                    else "degraded"
                    if grant_count and not permission_count
                    else "healthy"
                ),
                last_change=role.updated_at,
                detail_url=f"/admin/system/roles/{role.id}/edit",
            )
        )
    permission_count = (
        db.query(Permission).filter(Permission.is_active.is_(True)).count()
    )
    entries.insert(
        0,
        _entry(
            key="permission-catalogue",
            label="Active permission catalogue",
            value=f"{permission_count} grantable permissions",
            source="permissions database seeded by scripts/seed/seed_rbac.py",
            precedence="route requirement → active catalogue entry → grant evaluation",
            scope="all authenticated admin/API requests",
            health="healthy" if permission_count else "critical",
            last_change=db.query(func.max(Permission.updated_at)).scalar(),
            detail_url="/admin/system/permissions",
        ),
    )
    return entries


def _session_entries(db: Session) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    active_query = db.query(AuthSession).filter(
        AuthSession.status == SessionStatus.active, AuthSession.expires_at > now
    )
    active_count = active_query.count()
    latest_seen = active_query.with_entities(
        func.max(AuthSession.last_seen_at)
    ).scalar()
    entries = [
        _entry(
            key="auth-sessions",
            label="API and admin authentication sessions",
            value=f"{active_count} active",
            source="sessions database via session_manager",
            precedence="revocation/status → expiry → token rotation → authenticated",
            scope="subscriber, system-user, and reseller principals",
            health="healthy",
            last_change=latest_seen,
            detail_url="/admin/system/users/profile",
        )
    ]
    try:
        redis = redis_health_check()
        available = bool(redis.get("available"))
        last_check = redis.get("checked_at")
    except Exception:
        logger.warning("Control plane Redis health check failed", exc_info=True)
        available = False
        last_check = None
    for key, label, ttl_key in (
        ("customer-portal", "Customer portal sessions", "customer_session_ttl_seconds"),
        ("reseller-portal", "Reseller portal sessions", "reseller_session_ttl_seconds"),
    ):
        ttl = settings_spec.resolve_value(db, SettingDomain.auth, ttl_key)
        entries.append(
            _entry(
                key=key,
                label=label,
                value=f"Redis-backed; TTL {ttl} seconds",
                source="Redis session store",
                precedence="revocation marker → absolute TTL → idle TTL → active",
                scope=label.lower(),
                health="healthy" if available else "critical",
                last_change=last_check,
            )
        )
    return entries


def _scheduler_entries(db: Session) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    entries = []
    for task in db.query(ScheduledTask).order_by(ScheduledTask.name).all():
        schedule_type = _enum_value(task.schedule_type)
        cadence = (
            task.cron_expr or "missing cron"
            if task.schedule_type == ScheduleType.crontab
            else f"every {task.interval_seconds} seconds"
        )
        health = "disabled" if not task.enabled else "unknown"
        if task.enabled and task.last_run_at is not None:
            health = "healthy"
            if (
                task.schedule_type == ScheduleType.interval
                and task.interval_seconds
                and now - task.last_run_at
                > timedelta(seconds=task.interval_seconds * 2)
            ):
                health = "degraded"
        entries.append(
            _entry(
                key=f"task:{task.name}",
                label=task.name,
                value=f"{'Enabled' if task.enabled else 'Disabled'}; {schedule_type} {cadence}",
                source="scheduled_tasks database via scheduler_config",
                precedence="control registry/settings → ScheduledTask.enabled → beat dispatch",
                scope=task.task_name,
                health=health,
                last_change=task.updated_at,
                detail_url=f"/admin/system/scheduler/{task.id}",
            )
        )
    return entries


def _secret_entries() -> list[dict[str, object]]:
    try:
        data = build_secrets_index_context(status=None, message=None)
    except Exception:
        logger.warning("Control plane OpenBao inspection failed", exc_info=True)
        data = {"openbao_available": False, "secrets_list": []}
    available = bool(data.get("openbao_available"))
    entries = [
        _entry(
            key="openbao",
            label="OpenBao secret store",
            value="Available" if available else "Unavailable",
            source="OpenBao health API",
            precedence="OpenBao → environment fallback → application default",
            scope="all bao:// references and secret-aware configuration",
            health="healthy" if available else "critical",
            last_change=None,
            detail_url="/admin/system/secrets",
        )
    ]
    for secret in data.get("secrets_list", []):
        entries.append(
            _entry(
                key=f"secret:{secret['path']}",
                label=str(secret["path"]),
                value=f"Configured ({secret['field_count']} fields; values masked)",
                source="OpenBao KV metadata",
                precedence="path version current_version",
                scope=f"secret/{secret['path']}",
                health="healthy",
                last_change=secret.get("updated_time") or secret.get("created_time"),
                detail_url=f"/admin/system/secrets/{secret['path']}/edit",
                secret=False,
            )
        )
    return entries


def _integration_entries(db: Session) -> list[dict[str, object]]:
    try:
        rows = cast(
            list[dict[str, object]],
            build_installed_integrations_data(db).get("integrations", []),
        )
    except Exception:
        logger.warning("Control plane integration inspection failed", exc_info=True)
        rows = []
    entries = []
    for row in rows:
        installation = cast(IntegrationInstallation, row["installation"])
        stats = cast(dict[str, object], row.get("health_stats") or {})
        last_run_at = stats.get("last_run_at")
        entries.append(
            _entry(
                key=f"installation:{installation.id}",
                label=str(row.get("title") or installation.name),
                value=(
                    f"{installation.state.title()}; "
                    f"{installation.connector_key} / {installation.connector_version}"
                ),
                source="integration_installations database",
                precedence=(
                    "manifest pin → current config revision → enabled capability binding"
                ),
                scope=str(row.get("root") or "integrations"),
                health=(
                    "disabled"
                    if installation.state != "enabled"
                    else str(row["health"])
                ),
                last_change=last_run_at or installation.updated_at,
                detail_url=str(
                    row.get("manage_url") or "/admin/integrations/installed"
                ),
            )
        )
    return entries


def _webhook_entries(db: Session) -> list[dict[str, object]]:
    since = datetime.now(UTC) - timedelta(days=7)
    delivery_stats = {
        row.installation_id: (
            int(row.total or 0),
            int(row.failed or 0),
            row.last_attempt,
        )
        for row in db.query(
            IntegrationCapabilityBinding.installation_id,
            func.count(IntegrationDelivery.id).label("total"),
            func.sum(
                case(
                    (IntegrationDelivery.state == "dead_letter", 1),
                    else_=0,
                )
            ).label("failed"),
            func.max(IntegrationDelivery.last_attempt_at).label("last_attempt"),
        )
        .join(
            IntegrationDelivery,
            IntegrationDelivery.capability_binding_id
            == IntegrationCapabilityBinding.id,
        )
        .filter(IntegrationDelivery.created_at >= since)
        .group_by(IntegrationCapabilityBinding.installation_id)
        .all()
    }
    entries = []
    endpoints = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.connector_key == "webhook.http",
            IntegrationInstallation.state != "retired",
        )
        .order_by(IntegrationInstallation.name)
        .all()
    )
    for endpoint in endpoints:
        binding_ids = [binding.id for binding in endpoint.capability_bindings]
        subscriptions = (
            db.query(IntegrationEventSubscription)
            .filter(IntegrationEventSubscription.capability_binding_id.in_(binding_ids))
            .all()
            if binding_ids
            else []
        )
        active_subscriptions = sum(
            1 for item in subscriptions if item.state == "enabled"
        )
        total, failed, last_attempt = delivery_stats.get(endpoint.id, (0, 0, None))
        is_active = endpoint.state == "enabled"
        health = "disabled" if not is_active else "unknown"
        if is_active and total:
            health = "degraded" if failed / total >= 0.10 else "healthy"
        entries.append(
            _entry(
                key=f"webhook:{endpoint.id}",
                label=endpoint.name,
                value=(
                    f"{'Enabled' if is_active else endpoint.state.title()}; "
                    f"{active_subscriptions}/{len(subscriptions)} subscriptions active"
                ),
                source="integration installations/subscriptions/deliveries",
                precedence="installation enabled → capability bound → subscription enabled → retry policy",
                scope=f"{active_subscriptions} subscribed event types",
                health=health,
                last_change=last_attempt or endpoint.updated_at,
                detail_url=f"/admin/integrations/webhooks/{endpoint.id}",
            )
        )
    return entries


def _audit_history(db: Session) -> dict[str, list[dict[str, object]]]:
    history: dict[str, list[dict[str, object]]] = {
        section: [] for section in _AUDIT_TERMS
    }
    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.is_active.is_(True))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(250)
        .all()
    )
    for event in events:
        haystack = " ".join(
            [event.action or "", event.entity_type or "", event.entity_id or ""]
        ).lower()
        item = {
            "occurred_at": event.occurred_at,
            "action": event.action,
            "entity_type": event.entity_type,
            "actor": event.actor_id or _enum_value(event.actor_type),
            "success": event.is_success,
        }
        for section, terms in _AUDIT_TERMS.items():
            if len(history[section]) < 8 and any(term in haystack for term in terms):
                history[section].append(item)
    return history


def build_control_plane_context(db: Session) -> dict[str, object]:
    history = _audit_history(db)
    actor_labels = resolve_actor_display_names(
        db,
        {
            event["actor"]
            for events in history.values()
            for event in events
            if event.get("actor")
        },
    )
    for events in history.values():
        for event in events:
            event["actor_name"] = actor_labels.get(
                str(event.get("actor") or ""), "System"
            )
            event["action_label"] = humanize_action(str(event.get("action") or ""))
    section_data: list[tuple[str, str, str, list[dict[str, object]]]] = [
        (
            "settings",
            "Settings & controls",
            "Registered settings and canonical module/capability decisions.",
            _settings_entries(db),
        ),
        (
            "rbac",
            "RBAC",
            "Effective role catalogue, grants, scopes, and deny precedence.",
            _rbac_entries(db),
        ),
        (
            "sessions",
            "Sessions",
            "Database and Redis-backed authentication session owners.",
            _session_entries(db),
        ),
        (
            "scheduler",
            "Scheduler",
            "Canonical scheduled-task state and recent execution health.",
            _scheduler_entries(db),
        ),
        (
            "secrets",
            "Secrets",
            "OpenBao availability and metadata only; values are never exposed.",
            _secret_entries(),
        ),
        (
            "integrations",
            "Integrations",
            "Connector state and health derived from runs and deliveries.",
            _integration_entries(db),
        ),
        (
            "webhooks",
            "Webhooks",
            "Endpoint/subscription state and seven-day delivery health.",
            _webhook_entries(db),
        ),
    ]
    sections: list[dict[str, object]] = []
    all_entries: list[dict[str, object]] = []
    for section_id, name, description, entries in section_data:
        all_entries.extend(entries)
        sections.append(
            {
                "id": section_id,
                "name": name,
                "description": description,
                "entries": entries,
                "history": history[section_id],
                "health_counts": {
                    status: sum(1 for entry in entries if entry["health"] == status)
                    for status in (
                        "healthy",
                        "degraded",
                        "critical",
                        "disabled",
                        "unknown",
                    )
                },
            }
        )
    return {
        "sections": sections,
        "stats": {
            "sections": len(sections),
            "controls": len(all_entries),
            "healthy": sum(1 for entry in all_entries if entry["health"] == "healthy"),
            "attention": sum(
                1
                for entry in all_entries
                if entry["health"] in {"degraded", "critical"}
            ),
            "unknown": sum(1 for entry in all_entries if entry["health"] == "unknown"),
        },
        "generated_at": datetime.now(UTC),
    }
