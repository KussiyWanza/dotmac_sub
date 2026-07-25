from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.owner_commands import owner_command_active
from app.timezone import APP_TIMEZONE

logger = logging.getLogger(__name__)

SENSITIVE_FIELDS = {
    "nin",
    "password",
    "password_hash",
    "hashed_password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
    "salt",
}

# Keys containing any of these tokens are treated as sensitive.
SENSITIVE_FIELD_TOKENS = {
    "nin",
    "password",
    "secret",
    "token",
    "api_key",
    "private_key",
    "community",
    "credential",
}

AUDIT_TIMEZONE = APP_TIMEZONE


def _normalize_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_FIELDS:
        return True
    return any(token in lowered for token in SENSITIVE_FIELD_TOKENS)


def _to_audit_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(AUDIT_TIMEZONE)


def format_audit_datetime(value: datetime | None, fmt: str) -> str:
    localized = _to_audit_timezone(value)
    if not localized:
        return "Unknown"
    return localized.strftime(fmt)


def model_to_dict(
    model, include: set[str] | None = None, exclude: set[str] | None = None
) -> dict:
    if model is None:
        return {}
    excluded = set(exclude or set()) | SENSITIVE_FIELDS
    data: dict[str, object] = {}
    for attr in inspect(model).mapper.column_attrs:
        key = attr.key
        if include and key not in include:
            continue
        if key in excluded or _is_sensitive_key(key):
            continue
        data[key] = _normalize_value(getattr(model, key))
    return data


def diff_dicts(before: dict, after: dict) -> dict:
    changes: dict[str, dict[str, object]] = {}
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        before_val = before.get(key)
        after_val = after.get(key)
        if before_val != after_val:
            changes[key] = {"from": before_val, "to": after_val}
    return changes


def build_changes_metadata(
    before_model,
    after_model,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> dict | None:
    before = model_to_dict(before_model, include=include, exclude=exclude)
    after = model_to_dict(after_model, include=include, exclude=exclude)
    changes = diff_dicts(before, after)
    if not changes:
        return None
    return {"changes": changes}


def format_changes(changes: dict | None, max_items: int = 3) -> str | None:
    if not changes:
        return None
    items = []
    for idx, (field, value) in enumerate(changes.items()):
        if idx >= max_items:
            break
        if isinstance(value, Mapping):
            items.append(f"{field}: {value.get('from')} -> {value.get('to')}")
        else:
            # "created" events (and legacy rows) store the field snapshot
            # directly — {field: value} — rather than a {"from","to"} pair.
            # Render the value as-is instead of 500ing the whole detail page.
            items.append(f"{field}: {value}")
    if not items:
        return None
    suffix = "…" if len(changes) > max_items else ""
    return "; ".join(items) + suffix


def extract_changes(
    metadata: Mapping[str, Any] | None, action: str | None = None
) -> dict[str, Any] | None:
    if not metadata:
        return None
    changes = metadata.get("changes")
    if isinstance(changes, dict) and changes:
        return dict(changes)
    if "from" in metadata and "to" in metadata:
        field = "value"
        if action == "status_change":
            field = "status"
        elif action == "priority_change":
            field = "priority"
        return {field: {"from": metadata.get("from"), "to": metadata.get("to")}}
    return None


def humanize_action(action: str | None) -> str:
    if not action:
        return "Activity"
    method_map = {
        "GET": "Viewed",
        "POST": "Created",
        "PUT": "Updated",
        "PATCH": "Updated",
        "DELETE": "Deleted",
    }
    if action.upper() in method_map:
        return method_map[action.upper()]
    return action.replace("_", " ").replace("-", " ").title()


def _is_uuid_like(part: str) -> bool:
    """Check if a URL segment looks like a UUID (hex with optional dashes, 8+ chars)."""
    stripped = part.replace("-", "")
    return len(stripped) >= 8 and all(c in "0123456789abcdefABCDEF" for c in stripped)


def humanize_entity(entity_type: str | None, entity_id: str | None = None) -> str:
    if not entity_type:
        return "Item"
    label = entity_type
    if entity_type.startswith("/"):
        parts = [p for p in entity_type.split("/") if p]
        cleaned = []
        for part in parts:
            if part in {"admin", "api", "system", "portal", "vendor", "reseller"}:
                continue
            if part.isdigit() or _is_uuid_like(part):
                continue
            cleaned.append(part)
        label = " ".join(cleaned) if cleaned else entity_type.strip("/")
    label = label.replace("_", " ").replace("-", " ").title()
    if entity_id:
        short_id = str(entity_id)[:8]
        return f"{label} #{short_id}"
    return label


def log_audit_event(
    db: Session,
    request,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor_id: str | None,
    metadata: dict | None = None,
    status_code: int = 200,
    is_success: bool = True,
) -> None:
    resolved_actor_id = _resolve_request_actor_id(request, actor_id)
    actor_type = _resolve_request_actor_type(request, resolved_actor_id)
    metadata_payload = dict(metadata or {})
    actor_name, actor_email = _request_actor_identity(request)
    if actor_name and not metadata_payload.get("actor_name"):
        metadata_payload["actor_name"] = actor_name
    if actor_email and not metadata_payload.get("actor_email"):
        metadata_payload["actor_email"] = actor_email
    # A request only carries a name for interactive user actions. Background
    # jobs, tasks, event handlers, webhooks and API-key callers have no request,
    # so without this they would record a bare id. Snapshot a human label at
    # write time from the id itself, so the log reads correctly even after the
    # referenced user is later deleted (actor_id is not a foreign key).
    if not metadata_payload.get("actor_name") and resolved_actor_id:
        db_label = _resolve_actor_label_from_db(db, resolved_actor_id, actor_type)
        if db_label:
            metadata_payload["actor_name"] = db_label
    # The resolved label is also stored in its own column so audit can be
    # searched by person without a join and without parsing JSON.
    actor_label = metadata_payload.get("actor_name") or metadata_payload.get(
        "actor_email"
    )
    payload = AuditEventCreate(
        actor_type=actor_type,
        actor_id=resolved_actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        status_code=status_code,
        is_success=is_success,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        request_id=request.headers.get("x-request-id") if request else None,
        metadata_=metadata_payload or None,
    )
    if owner_command_active(db):
        audit_service.audit_events.stage(db=db, payload=payload)
    else:
        audit_service.audit_events.create(db=db, payload=payload)


def _resolve_request_actor_id(request, actor_id: str | None) -> str | None:
    """Prefer explicit IDs, then authenticated request principal IDs."""
    for candidate in (
        actor_id,
        getattr(getattr(request, "state", None), "actor_id", None)
        if request is not None
        else None,
        _request_auth_value(request, "principal_id"),
        _request_auth_value(request, "subscriber_id"),
        _request_user_value(request, "id"),
        _cached_current_user_value(request, "actor_id"),
        _cached_current_user_value(request, "principal_id"),
        _cached_current_user_value(request, "id"),
        _cached_current_user_value(request, "subscriber_id"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _resolve_request_actor_type(request, actor_id: str | None) -> AuditActorType:
    raw_type = _request_auth_value(request, "principal_type") or getattr(
        getattr(request, "state", None), "actor_type", None
    )
    normalized = str(raw_type or "").strip().lower()
    if normalized in {AuditActorType.api_key.value, AuditActorType.service.value}:
        return AuditActorType(normalized)
    if normalized in {"subscriber", "system_user", "reseller_user", "user"}:
        return AuditActorType.user
    return AuditActorType.user if actor_id else AuditActorType.system


def _request_auth_value(request, key: str):
    if request is None:
        return None
    auth = getattr(getattr(request, "state", None), "auth", None)
    if isinstance(auth, Mapping):
        return auth.get(key)
    return None


def _request_user_value(request, key: str):
    if request is None:
        return None
    user = getattr(getattr(request, "state", None), "user", None)
    if user is None:
        return None
    if isinstance(user, Mapping):
        return user.get(key)
    try:
        return getattr(user, key, None)
    except Exception:
        return None


def _cached_current_user_value(request, key: str):
    if request is None:
        return None
    cached = getattr(
        getattr(request, "state", None), "_dotmac_cached_user_context", None
    )
    if isinstance(cached, Mapping):
        return cached.get(key)
    return None


def _request_actor_identity(request) -> tuple[str | None, str | None]:
    """Return display name and email from request state without requiring DB IO."""
    if request is None:
        return None, None

    cached_name = _first_text(
        _cached_current_user_value(request, "name"),
        _cached_current_user_value(request, "display_name"),
    )
    cached_email = _first_text(_cached_current_user_value(request, "email"))
    if cached_name or cached_email:
        return cached_name, cached_email

    user = getattr(getattr(request, "state", None), "user", None)
    if user is None:
        return None, None
    if isinstance(user, Mapping):
        first_name = _first_text(user.get("first_name"))
        last_name = _first_text(user.get("last_name"))
        display_name = _first_text(
            user.get("display_name"),
            user.get("name"),
            f"{first_name or ''} {last_name or ''}",
        )
        return display_name, _first_text(user.get("email"))

    try:
        state = getattr(user, "__dict__", {}) or {}
        first_name = _first_text(
            state.get("first_name"), getattr(user, "first_name", None)
        )
        last_name = _first_text(
            state.get("last_name"), getattr(user, "last_name", None)
        )
        display_name = _first_text(
            state.get("display_name"),
            getattr(user, "display_name", None),
            getattr(user, "name", None),
            f"{first_name or ''} {last_name or ''}",
        )
        email = _first_text(state.get("email"), getattr(user, "email", None))
        return display_name, email
    except Exception:
        return None, None


def _first_text(*values) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def build_audit_activities(
    db: Session,
    entity_type: str,
    entity_id: str,
    limit: int = 10,
) -> list[dict]:
    """Build activity feed for a single entity type + id."""
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    return _events_to_activities(db, events)


def build_audit_activities_for_types(
    db: Session,
    entity_types: list[str],
    limit: int = 10,
) -> list[dict]:
    """Build activity feed across multiple entity types."""
    if not entity_types:
        return []
    from app.models.audit import AuditEvent

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type.in_(entity_types))
        .filter(AuditEvent.is_active.is_(True))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    return _events_to_activities(db, events, include_entity_label=True)


def list_audit_events_for_entities(
    db: Session,
    entity_refs: list[tuple[str, str]],
    limit: int = 20,
) -> list:
    """Return recent audit events for the provided entity type/id pairs."""
    if not entity_refs:
        return []

    from app.models.audit import AuditEvent

    normalized_refs = list(
        dict.fromkeys(
            (str(entity_type), str(entity_id))
            for entity_type, entity_id in entity_refs
            if entity_type and entity_id
        )
    )
    if not normalized_refs:
        return []

    ref_filters = [
        and_(
            AuditEvent.entity_type == entity_type,
            AuditEvent.entity_id == entity_id,
        )
        for entity_type, entity_id in normalized_refs
    ]
    predicate = ref_filters[0] if len(ref_filters) == 1 else or_(*ref_filters)
    return (
        db.query(AuditEvent)
        .filter(AuditEvent.is_active.is_(True))
        .filter(predicate)
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )


def load_audit_actor_subscribers(db: Session, events: list) -> dict[str, object]:
    """Resolve the actors referenced by the provided audit events.

    Covers user actors (Subscriber/SystemUser) and api_key actors (ApiKey), so
    historical events written before the label was snapshotted still display a
    name at read time instead of a raw id.
    """
    user_ids: set[str] = set()
    api_key_ids: set[str] = set()
    for event in events:
        actor_id = getattr(event, "actor_id", None)
        if not actor_id:
            continue
        actor_type = getattr(event, "actor_type", None)
        try:
            normalized = str(UUID(str(actor_id)))
        except (TypeError, ValueError):
            continue
        if _is_user_actor(actor_type):
            user_ids.add(normalized)
        elif _is_api_key_actor(actor_type):
            api_key_ids.add(normalized)
    actors: dict[str, object] = {}
    if user_ids:
        actors.update(
            {
                str(subscriber.id): subscriber
                for subscriber in db.query(Subscriber)
                .filter(Subscriber.id.in_(user_ids))
                .all()
            }
        )
        actors.update(
            {
                str(user.id): user
                for user in db.query(SystemUser)
                .filter(SystemUser.id.in_(user_ids))
                .all()
            }
        )
    if api_key_ids:
        actors.update(
            {
                str(key.id): key
                for key in db.query(ApiKey).filter(ApiKey.id.in_(api_key_ids)).all()
            }
        )
    return actors


def _events_to_activities(
    db: Session,
    events: list,
    include_entity_label: bool = False,
) -> list[dict]:
    """Shared logic: resolve actors and build activity dicts from audit events."""
    if not events:
        return []
    people = load_audit_actor_subscribers(db, events)
    activities: list[dict] = []
    for event in events:
        actor_name = _resolve_actor_name(event, people)
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        action_label = (event.action or "Activity").replace("_", " ").title()
        if include_entity_label:
            entity_label = (event.entity_type or "Activity").replace("_", " ").title()
            title = f"{entity_label} {action_label}"
        else:
            title = action_label
        if comment_text:
            description = f"{actor_name} · {comment_text}"
        else:
            description = f"{actor_name}" + (
                f" · {change_summary}" if change_summary else ""
            )
        activities.append(
            {
                "title": title,
                "description": description,
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def log_update(
    db: Session,
    request,
    entity_type: str,
    entity_id: str,
    before_obj,
    after_obj,
    actor_id: str | None,
    exclude_fields: set[str] | None = None,
) -> None:
    """Snapshot before/after, compute diff, and log an audit event in one call."""
    meta = build_changes_metadata(before_obj, after_obj, exclude=exclude_fields)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        metadata=meta,
    )


def _is_user_actor(actor_type) -> bool:
    return actor_type in {AuditActorType.user, AuditActorType.user.value, "user"}


def _is_api_key_actor(actor_type) -> bool:
    return actor_type in {
        AuditActorType.api_key,
        AuditActorType.api_key.value,
        "api_key",
    }


def _label_from_actor_object(actor: object) -> str | None:
    """A human label for a resolved actor row (person or API key)."""
    if actor is None:
        return None
    label = getattr(actor, "label", None)  # ApiKey
    if label:
        return str(label)
    display_name = getattr(actor, "display_name", None)
    if display_name:
        return str(display_name)
    first = str(getattr(actor, "first_name", "") or "").strip()
    last = str(getattr(actor, "last_name", "") or "").strip()
    full = f"{first} {last}".strip()
    return full or getattr(actor, "email", None)


def _resolve_actor_label_from_db(db, actor_id, actor_type) -> str | None:
    """Look up a human label for an actor id at write time.

    Handles the actor types whose id is an opaque UUID (user, api_key). Service
    actors already carry a readable id (e.g. "system:outage-classifier"), so
    they are left to display as-is.
    """
    if db is None or not actor_id:
        return None
    try:
        pk = UUID(str(actor_id))
    except (TypeError, ValueError):
        return None
    if _is_api_key_actor(actor_type):
        return _label_from_actor_object(db.get(ApiKey, pk))
    if _is_user_actor(actor_type):
        return _label_from_actor_object(
            db.get(SystemUser, pk)
        ) or _label_from_actor_object(db.get(Subscriber, pk))
    return None


def resolve_actor_label_from_db(
    db: Session,
    actor_id: str,
    actor_type: AuditActorType,
) -> str | None:
    """Resolve the canonical write-time label snapshot for an audit actor."""

    return _resolve_actor_label_from_db(db, actor_id, actor_type)


def _resolve_actor_name(event, subscribers: dict[str, object]) -> str:
    # Prefer the label stored at write time — it is authoritative and survives
    # deletion of the actor. Live resolution below only backfills older rows.
    stored_label = getattr(event, "actor_label", None)
    if stored_label:
        return str(stored_label)
    actor_id = getattr(event, "actor_id", None)
    actor_type = getattr(event, "actor_type", None)
    if actor_id and (_is_user_actor(actor_type) or _is_api_key_actor(actor_type)):
        label = _label_from_actor_object(subscribers.get(str(actor_id)))
        if label:
            return label
        metadata = getattr(event, "metadata_", None) or {}
        return (
            metadata.get("actor_name") or metadata.get("actor_email") or str(actor_id)
        )
    metadata = getattr(event, "metadata_", None) or {}
    return (
        metadata.get("actor_name")
        or metadata.get("actor_email")
        or (str(actor_id) if actor_id else None)
        or "System"
    )


def resolve_actor_name(event, subscribers: dict[str, object]) -> str:
    return _resolve_actor_name(event, subscribers)


def build_recent_activity_feed(db: Session, events: list, limit: int = 5) -> list[dict]:
    if not events:
        return []
    sliced_events = events[:limit]
    subscribers = load_audit_actor_subscribers(db, sliced_events)
    activities = []
    for event in sliced_events:
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(getattr(event, "action", None))
        entity_label = humanize_entity(
            getattr(event, "entity_type", None), getattr(event, "entity_id", None)
        )
        actor_name = _resolve_actor_name(event, subscribers)
        time_str = format_audit_datetime(
            getattr(event, "occurred_at", None), "%b %d, %H:%M"
        )
        message = f"{actor_name} {action_label} {entity_label}"
        detail = change_summary or entity_label
        activities.append(
            {
                "message": message,
                "detail": detail,
                "time": time_str,
            }
        )
    return activities


def recent_activity_for_paths(
    db: Session,
    path_prefixes: list[str],
    limit: int = 5,
    fetch_limit: int = 200,
) -> list[dict]:
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=fetch_limit,
        offset=0,
    )
    if not events:
        return []
    filtered = []
    prefixes = tuple(path_prefixes)
    for event in events:
        entity_type = getattr(event, "entity_type", "") or ""
        if entity_type.startswith(prefixes):
            filtered.append(event)
            if len(filtered) >= limit:
                break
    return build_recent_activity_feed(db, filtered, limit=limit)
