from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxComment,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxLabel,
    InboxMessage,
    InboxMessageTemplate,
    InboxReplyMacro,
    InboxSavedFilter,
)
from app.services import team_inbox_assignment, team_inbox_outbound
from app.services.common import coerce_uuid

_ALLOWED_LABEL_COLORS = {
    "slate",
    "blue",
    "indigo",
    "violet",
    "emerald",
    "teal",
    "amber",
    "orange",
    "rose",
    "red",
}


class InboxOperationError(ValueError):
    pass


@dataclass(frozen=True)
class LabelOption:
    id: str
    name: str
    slug: str
    color: str
    usage_count: int = 0


@dataclass(frozen=True)
class MacroOption:
    id: str
    name: str
    description: str | None
    body_text: str
    visibility: str
    actions: list[dict[str, Any]]
    execution_count: int


@dataclass(frozen=True)
class MessageTemplateOption:
    id: str
    name: str
    channel_type: str
    subject: str | None
    body_text: str
    body_html: str | None


@dataclass(frozen=True)
class SavedFilterOption:
    id: str
    name: str
    filter_payload: dict[str, Any]
    is_shared: bool
    owner_person_id: str | None


@dataclass(frozen=True)
class InboxQueueMetrics:
    total_open: int
    needs_response: int
    failed_outbound: int
    unassigned_open: int
    muted_open: int
    snoozed_open: int


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100] if slug else "label"


def _label_name(value: str | None) -> str:
    name = str(value or "").strip()
    if not name:
        raise InboxOperationError("Label name is required.")
    if len(name) > 80:
        raise InboxOperationError("Label name must be 80 characters or fewer.")
    return name


def _label_color(value: str | None) -> str:
    color = str(value or "").strip().lower()
    return color if color in _ALLOWED_LABEL_COLORS else "slate"


def list_labels(db: Session, *, active_only: bool = True) -> list[LabelOption]:
    query = db.query(InboxLabel)
    if active_only:
        query = query.filter(InboxLabel.is_active.is_(True))
    usage_rows = (
        db.query(
            InboxConversationLabel.label_id,
            func.count(InboxConversationLabel.id).label("usage_count"),
        )
        .filter(InboxConversationLabel.is_active.is_(True))
        .group_by(InboxConversationLabel.label_id)
        .all()
    )
    usage_by_label = {row.label_id: int(row.usage_count or 0) for row in usage_rows}
    return [
        LabelOption(
            id=str(label.id),
            name=label.name,
            slug=label.slug,
            color=label.color or "slate",
            usage_count=usage_by_label.get(label.id, 0),
        )
        for label in query.order_by(func.lower(InboxLabel.name).asc()).all()
    ]


def conversation_labels(
    db: Session,
    conversation_id: str | UUID,
) -> list[LabelOption]:
    rows = (
        db.query(InboxConversationLabel, InboxLabel)
        .join(InboxLabel, InboxLabel.id == InboxConversationLabel.label_id)
        .filter(InboxConversationLabel.conversation_id == coerce_uuid(conversation_id))
        .filter(InboxConversationLabel.is_active.is_(True))
        .filter(InboxLabel.is_active.is_(True))
        .order_by(func.lower(InboxLabel.name).asc())
        .all()
    )
    return [
        LabelOption(
            id=str(label.id),
            name=label.name,
            slug=label.slug,
            color=label.color or "slate",
        )
        for _link, label in rows
    ]


def create_or_reactivate_label(
    db: Session,
    *,
    name: str,
    color: str | None = None,
) -> InboxLabel:
    normalized_name = _label_name(name)
    label = (
        db.query(InboxLabel)
        .filter(func.lower(InboxLabel.name) == normalized_name.lower())
        .first()
    )
    if label is None:
        label = InboxLabel(
            name=normalized_name,
            slug=_slugify(normalized_name),
            color=_label_color(color),
            is_active=True,
        )
        db.add(label)
    else:
        label.name = normalized_name
        label.slug = _slugify(normalized_name)
        label.color = _label_color(color)
        label.is_active = True
    db.flush()
    return label


def apply_label(
    db: Session,
    *,
    conversation: InboxConversation,
    label_id: str | UUID,
    applied_by_person_id: str | UUID | None = None,
) -> InboxConversationLabel:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None or not label.is_active:
        raise InboxOperationError("Label not found.")
    existing = (
        db.query(InboxConversationLabel)
        .filter(InboxConversationLabel.conversation_id == conversation.id)
        .filter(InboxConversationLabel.label_id == label.id)
        .order_by(InboxConversationLabel.created_at.desc())
        .first()
    )
    if existing is not None:
        existing.is_active = True
        existing.applied_by_person_id = coerce_uuid(applied_by_person_id)
        db.flush()
        return existing
    link = InboxConversationLabel(
        conversation_id=conversation.id,
        label_id=label.id,
        applied_by_person_id=coerce_uuid(applied_by_person_id),
        is_active=True,
    )
    db.add(link)
    db.flush()
    return link


def remove_label(
    db: Session,
    *,
    conversation: InboxConversation,
    label_id: str | UUID,
) -> None:
    link = (
        db.query(InboxConversationLabel)
        .filter(InboxConversationLabel.conversation_id == conversation.id)
        .filter(InboxConversationLabel.label_id == coerce_uuid(label_id))
        .filter(InboxConversationLabel.is_active.is_(True))
        .first()
    )
    if link is not None:
        link.is_active = False
        db.flush()


def update_label(
    db: Session,
    *,
    label_id: str | UUID,
    name: str,
    color: str | None = None,
    is_active: bool = True,
) -> InboxLabel:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None:
        raise InboxOperationError("Label not found.")
    normalized_name = _label_name(name)
    duplicate = (
        db.query(InboxLabel)
        .filter(func.lower(InboxLabel.name) == normalized_name.lower())
        .filter(InboxLabel.id != label.id)
        .first()
    )
    if duplicate is not None:
        raise InboxOperationError("A label with that name already exists.")
    label.name = normalized_name
    label.slug = _slugify(normalized_name)
    label.color = _label_color(color)
    label.is_active = bool(is_active)
    db.flush()
    return label


def delete_label(db: Session, *, label_id: str | UUID) -> None:
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None:
        raise InboxOperationError("Label not found.")
    label.is_active = False
    db.flush()


def list_macros(
    db: Session,
    *,
    person_id: str | UUID | None = None,
    active_only: bool = True,
) -> list[MacroOption]:
    person_uuid = coerce_uuid(person_id)
    query = db.query(InboxReplyMacro)
    if active_only:
        query = query.filter(InboxReplyMacro.is_active.is_(True))
    if person_uuid is not None:
        query = query.filter(
            or_(
                InboxReplyMacro.visibility == "shared",
                InboxReplyMacro.created_by_person_id == person_uuid,
            )
        )
    else:
        query = query.filter(InboxReplyMacro.visibility == "shared")
    return [
        MacroOption(
            id=str(macro.id),
            name=macro.name,
            description=macro.description,
            body_text=macro.body_text,
            visibility=macro.visibility,
            actions=[
                action for action in (macro.actions or []) if isinstance(action, dict)
            ],
            execution_count=macro.execution_count,
        )
        for macro in query.order_by(
            InboxReplyMacro.execution_count.desc(),
            func.lower(InboxReplyMacro.name).asc(),
        )
        .limit(200)
        .all()
    ]


def create_macro(
    db: Session,
    *,
    name: str,
    body_text: str,
    description: str | None = None,
    visibility: str = "shared",
    actions: list[dict[str, Any]] | None = None,
    created_by_person_id: str | UUID | None = None,
) -> InboxReplyMacro:
    clean_name = str(name or "").strip()
    clean_body = str(body_text or "").strip()
    clean_visibility = str(visibility or "shared").strip().lower()
    if clean_visibility not in {"shared", "personal"}:
        clean_visibility = "shared"
    if not clean_name:
        raise InboxOperationError("Macro name is required.")
    if not clean_body:
        raise InboxOperationError("Macro body is required.")
    clean_actions = _validate_macro_actions(
        actions or [{"action_type": "reply_text", "params": {"body_text": clean_body}}]
    )
    macro = InboxReplyMacro(
        name=clean_name[:120],
        description=str(description or "").strip()[:1000] or None,
        body_text=clean_body,
        visibility=clean_visibility,
        actions=clean_actions,
        created_by_person_id=coerce_uuid(created_by_person_id),
        is_active=True,
    )
    db.add(macro)
    db.flush()
    return macro


def _validate_macro_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_types = {
        "assign_conversation",
        "set_status",
        "add_tag",
        "reply_text",
        "send_template",
    }
    cleaned: list[dict[str, Any]] = []
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            raise InboxOperationError(f"Invalid macro action at index {idx}.")
        action_type = str(action.get("action_type") or "").strip()
        if action_type not in valid_types:
            raise InboxOperationError(
                f"Invalid macro action type at index {idx}: {action_type}."
            )
        params = action.get("params")
        if not isinstance(params, dict):
            raise InboxOperationError(f"Macro action {action_type} requires params.")
        cleaned.append({"action_type": action_type, "params": dict(params)})
    return cleaned


def execute_macro_actions(
    db: Session,
    *,
    conversation: InboxConversation,
    macro_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    macro = record_macro_use(db, macro_id)
    if macro is None:
        raise InboxOperationError("Macro not found.")

    actor_uuid = coerce_uuid(actor_person_id)
    executed = 0
    failed = 0
    results: list[dict[str, object]] = []
    for action in macro.actions or []:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "").strip()
        raw_params = action.get("params")
        params: dict[str, Any] = (
            dict(raw_params) if isinstance(raw_params, dict) else {}
        )
        try:
            if action_type == "set_status":
                status_value = str(params.get("status") or "").strip().lower()
                if status_value not in {"open", "pending", "snoozed", "resolved"}:
                    raise InboxOperationError("Unsupported conversation status.")
                metadata = dict(conversation.metadata_ or {})
                history = metadata.get("status_history")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "from": conversation.status,
                        "to": status_value,
                        "at": _now_iso(),
                        "actor_id": str(actor_uuid) if actor_uuid else None,
                        "source": "team_inbox_macro",
                        "macro_id": str(macro.id),
                    }
                )
                metadata["status_history"] = history[-50:]
                conversation.status = status_value
                conversation.metadata_ = metadata
            elif action_type == "add_tag":
                label_name = str(params.get("tag") or params.get("label") or "").strip()
                if not label_name:
                    raise InboxOperationError("Label name is required.")
                label = create_or_reactivate_label(db, name=label_name)
                apply_label(
                    db,
                    conversation=conversation,
                    label_id=label.id,
                    applied_by_person_id=actor_person_id,
                )
            elif action_type == "assign_conversation":
                metadata = dict(conversation.metadata_ or {})
                macro_assignments = metadata.get("macro_assignments")
                if not isinstance(macro_assignments, list):
                    macro_assignments = []
                macro_assignments.append(
                    {
                        "person_id": params.get("person_id") or params.get("agent_id"),
                        "service_team_id": params.get("service_team_id")
                        or params.get("team_id"),
                        "actor_id": str(actor_uuid) if actor_uuid else None,
                        "at": _now_iso(),
                        "macro_id": str(macro.id),
                    }
                )
                metadata["macro_assignments"] = macro_assignments[-20:]
                conversation.metadata_ = metadata
            elif action_type in {"reply_text", "send_template"}:
                # Reply dispatch remains explicit in the admin route so provider
                # errors are visible to the agent before the conversation changes.
                pass
            else:
                raise InboxOperationError(f"Unsupported macro action: {action_type}.")
            executed += 1
            results.append({"action": action_type, "ok": True})
        except Exception as exc:
            failed += 1
            results.append({"action": action_type, "ok": False, "error": str(exc)})
    db.flush()
    return {
        "ok": failed == 0,
        "actions_executed": executed,
        "actions_failed": failed,
        "results": results,
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def record_macro_use(
    db: Session, macro_id: str | UUID | None
) -> InboxReplyMacro | None:
    if not macro_id:
        return None
    try:
        macro_uuid = coerce_uuid(macro_id)
    except (TypeError, ValueError):
        return None
    macro = db.get(InboxReplyMacro, macro_uuid)
    if macro is None or not macro.is_active:
        return None
    macro.execution_count = int(macro.execution_count or 0) + 1
    db.flush()
    return macro


def list_templates(
    db: Session,
    *,
    channel_type: str | None = None,
    active_only: bool = True,
) -> list[MessageTemplateOption]:
    query = db.query(InboxMessageTemplate)
    if active_only:
        query = query.filter(InboxMessageTemplate.is_active.is_(True))
    if channel_type:
        query = query.filter(
            InboxMessageTemplate.channel_type.in_([channel_type, "any"])
        )
    return [
        MessageTemplateOption(
            id=str(template.id),
            name=template.name,
            channel_type=template.channel_type,
            subject=template.subject,
            body_text=template.body_text,
            body_html=template.body_html,
        )
        for template in query.order_by(func.lower(InboxMessageTemplate.name).asc())
        .limit(200)
        .all()
    ]


def create_template(
    db: Session,
    *,
    name: str,
    channel_type: str,
    body_text: str,
    subject: str | None = None,
    body_html: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> InboxMessageTemplate:
    clean_name = str(name or "").strip()
    clean_channel = str(channel_type or "any").strip().lower()
    clean_body = str(body_text or "").strip()
    if clean_channel not in {
        "any",
        "email",
        "whatsapp",
        "facebook_messenger",
        "instagram_dm",
        "chat_widget",
    }:
        clean_channel = "any"
    if not clean_name:
        raise InboxOperationError("Template name is required.")
    if not clean_body:
        raise InboxOperationError("Template body is required.")
    template = InboxMessageTemplate(
        name=clean_name[:160],
        channel_type=clean_channel,
        subject=str(subject or "").strip()[:200] or None,
        body_text=clean_body,
        body_html=str(body_html or "").strip() or None,
        metadata_=metadata or None,
        is_active=True,
    )
    db.add(template)
    db.flush()
    return template


def get_template(db: Session, template_id: str | UUID) -> InboxMessageTemplate:
    template = db.get(InboxMessageTemplate, coerce_uuid(template_id))
    if template is None or not template.is_active:
        raise InboxOperationError("Template not found.")
    return template


def bulk_update_status(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    status_value: str,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    clean_status = str(status_value or "").strip().lower()
    if clean_status not in {"open", "pending", "snoozed", "resolved"}:
        raise InboxOperationError("Unsupported conversation status.")
    actor_uuid = coerce_uuid(actor_person_id)
    updated: list[str] = []
    skipped: list[str] = []
    for raw_id in conversation_ids:
        conversation = db.get(InboxConversation, coerce_uuid(raw_id))
        if conversation is None or not conversation.is_active:
            skipped.append(str(raw_id))
            continue
        if conversation.status == clean_status:
            skipped.append(str(conversation.id))
            continue
        metadata = dict(conversation.metadata_ or {})
        history = metadata.get("status_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "from": conversation.status,
                "to": clean_status,
                "at": _now_iso(),
                "actor_id": str(actor_uuid) if actor_uuid else None,
                "source": "team_inbox_bulk_status",
            }
        )
        metadata["status_history"] = history[-50:]
        conversation.status = clean_status
        conversation.metadata_ = metadata
        updated.append(str(conversation.id))
    db.flush()
    return {"updated": updated, "skipped": skipped, "status": clean_status}


def bulk_update_priority(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    priority: int | None,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    if priority is None or not 0 <= int(priority) <= 999:
        raise InboxOperationError("Priority must be between 0 and 999.")
    actor_uuid = coerce_uuid(actor_person_id)
    updated: list[str] = []
    skipped: list[str] = []
    for raw_id in conversation_ids:
        conversation_uuid = coerce_uuid(raw_id)
        conversation = (
            db.query(InboxConversation)
            .filter(InboxConversation.id == conversation_uuid)
            .with_for_update()
            .one_or_none()
            if conversation_uuid is not None
            else None
        )
        if conversation is None or not conversation.is_active:
            skipped.append(str(raw_id))
            continue
        if conversation.priority == int(priority):
            skipped.append(str(conversation.id))
            continue
        metadata = dict(conversation.metadata_ or {})
        history = metadata.get("priority_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "from": conversation.priority,
                "to": int(priority),
                "at": _now_iso(),
                "actor_id": str(actor_uuid) if actor_uuid else None,
                "source": "team_inbox_bulk_priority",
            }
        )
        metadata["priority_history"] = history[-50:]
        conversation.priority = int(priority)
        conversation.metadata_ = metadata
        updated.append(str(conversation.id))
    db.flush()
    return {"updated": updated, "skipped": skipped, "priority": int(priority)}


def bulk_apply_label(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    label_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    updated: list[str] = []
    skipped: list[str] = []
    label = db.get(InboxLabel, coerce_uuid(label_id))
    if label is None or not label.is_active:
        raise InboxOperationError("Label not found.")
    for raw_id in conversation_ids:
        conversation = db.get(InboxConversation, coerce_uuid(raw_id))
        if conversation is None or not conversation.is_active:
            skipped.append(str(raw_id))
            continue
        apply_label(
            db,
            conversation=conversation,
            label_id=label.id,
            applied_by_person_id=actor_person_id,
        )
        updated.append(str(conversation.id))
    db.flush()
    return {"updated": updated, "skipped": skipped, "label_id": str(label.id)}


def update_conversation_workflow(
    db: Session,
    *,
    conversation: InboxConversation,
    priority: int | None = None,
    is_muted: bool | None = None,
    snooze_minutes: int | None = None,
    snooze_until: datetime | None = None,
    actor_person_id: str | UUID | None = None,
) -> InboxConversation:
    metadata = dict(conversation.metadata_ or {})
    history = metadata.get("workflow_history")
    if not isinstance(history, list):
        history = []
    event: dict[str, Any] = {
        "at": _now_iso(),
        "actor_id": str(coerce_uuid(actor_person_id))
        if coerce_uuid(actor_person_id)
        else None,
        "source": "team_inbox_workflow",
    }
    if priority is not None:
        clean_priority = max(0, min(int(priority), 999))
        event["priority"] = {"from": conversation.priority, "to": clean_priority}
        conversation.priority = clean_priority
    if is_muted is not None:
        event["is_muted"] = {"from": conversation.is_muted, "to": bool(is_muted)}
        conversation.is_muted = bool(is_muted)
    # An explicit wake time wins over a duration: the operator picked a moment,
    # not an interval, and a past moment is the same as "do not snooze".
    if snooze_until is not None or snooze_minutes is not None:
        if snooze_until is not None:
            target = (
                snooze_until
                if snooze_until.tzinfo
                else snooze_until.replace(tzinfo=UTC)
            )
            if target <= datetime.now(UTC):
                raise InboxOperationError("Choose a snooze time in the future.")
        elif int(snooze_minutes or 0) <= 0:
            target = None
        else:
            target = datetime.now(UTC) + timedelta(minutes=int(snooze_minutes or 0))
        event["snoozed_until"] = {
            "from": conversation.snoozed_until.isoformat()
            if conversation.snoozed_until
            else None,
            "to": target.isoformat() if target else None,
        }
        conversation.snoozed_until = target
        if target is not None:
            conversation.status = "snoozed"
    history.append(event)
    metadata["workflow_history"] = history[-50:]
    conversation.metadata_ = metadata
    db.flush()
    return conversation


def save_filter(
    db: Session,
    *,
    name: str,
    filter_payload: dict[str, Any],
    owner_person_id: str | UUID | None = None,
    is_shared: bool = False,
) -> InboxSavedFilter:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise InboxOperationError("Filter name is required.")
    saved_filter = InboxSavedFilter(
        name=clean_name[:120],
        filter_payload={
            key: value
            for key, value in filter_payload.items()
            if value not in (None, "")
        },
        owner_person_id=coerce_uuid(owner_person_id),
        is_shared=bool(is_shared),
        is_active=True,
    )
    db.add(saved_filter)
    db.flush()
    return saved_filter


def list_saved_filters(
    db: Session,
    *,
    person_id: str | UUID | None = None,
) -> list[SavedFilterOption]:
    person_uuid = coerce_uuid(person_id)
    query = db.query(InboxSavedFilter).filter(InboxSavedFilter.is_active.is_(True))
    if person_uuid is not None:
        query = query.filter(
            or_(
                InboxSavedFilter.is_shared.is_(True),
                InboxSavedFilter.owner_person_id == person_uuid,
            )
        )
    else:
        query = query.filter(InboxSavedFilter.is_shared.is_(True))
    return [
        SavedFilterOption(
            id=str(row.id),
            name=row.name,
            filter_payload=dict(row.filter_payload or {}),
            is_shared=row.is_shared,
            owner_person_id=str(row.owner_person_id) if row.owner_person_id else None,
        )
        for row in query.order_by(func.lower(InboxSavedFilter.name).asc())
        .limit(100)
        .all()
    ]


def delete_saved_filter(
    db: Session,
    *,
    filter_id: str | UUID,
) -> None:
    saved_filter = db.get(InboxSavedFilter, coerce_uuid(filter_id))
    if saved_filter is None:
        raise InboxOperationError("Saved filter not found.")
    saved_filter.is_active = False
    db.flush()


def create_comment(
    db: Session,
    *,
    conversation: InboxConversation,
    body: str,
    author_person_id: str | UUID | None = None,
    message_id: str | UUID | None = None,
    visibility: str = "internal",
) -> InboxComment:
    clean_body = str(body or "").strip()
    clean_visibility = str(visibility or "internal").strip().lower()
    if clean_visibility not in {"internal", "private"}:
        clean_visibility = "internal"
    if not clean_body:
        raise InboxOperationError("Comment body is required.")
    comment = InboxComment(
        conversation_id=conversation.id,
        message_id=coerce_uuid(message_id),
        author_person_id=coerce_uuid(author_person_id),
        body=clean_body,
        visibility=clean_visibility,
        is_resolved=False,
        metadata_={"source": "team_inbox_comment"},
    )
    db.add(comment)
    db.flush()
    return comment


def create_internal_note(
    db: Session,
    *,
    conversation: InboxConversation,
    body: str,
    actor_person_id: str | UUID | None = None,
) -> InboxMessage:
    clean_body = str(body or "").strip()
    if not clean_body:
        raise InboxOperationError("Internal note is required.")
    note = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction="internal",
        body=clean_body,
        from_address="internal",
        metadata_={
            "source": "admin_inbox_internal_note",
            "actor_id": str(actor_person_id) if actor_person_id else None,
        },
    )
    db.add(note)
    db.flush()
    return note


def resolve_comment(
    db: Session,
    *,
    comment_id: str | UUID,
    resolved_by_person_id: str | UUID | None = None,
) -> InboxComment:
    comment = db.get(InboxComment, coerce_uuid(comment_id))
    if comment is None:
        raise InboxOperationError("Comment not found.")
    comment.is_resolved = True
    comment.resolved_by_person_id = coerce_uuid(resolved_by_person_id)
    comment.resolved_at = datetime.now(UTC)
    db.flush()
    return comment


def bulk_escalate(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    service_team_id: str | UUID,
    assigned_person_id: str | UUID | None = None,
    auto_assign: bool = True,
    actor_person_id: str | UUID | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    for raw_id in conversation_ids:
        conversation = db.get(InboxConversation, coerce_uuid(raw_id))
        if conversation is None or not conversation.is_active:
            skipped.append({"conversation_id": str(raw_id), "reason": "not_found"})
            continue
        if conversation.status == "resolved":
            skipped.append(
                {"conversation_id": str(conversation.id), "reason": "resolved"}
            )
            continue
        if assigned_person_id:
            result = team_inbox_assignment.assign_conversation_to_agent(
                db,
                conversation=conversation,
                service_team_id=service_team_id,
                person_id=assigned_person_id,
                assigned_by_person_id=actor_person_id,
                reason=reason,
            )
        elif auto_assign:
            result = team_inbox_assignment.assign_conversation_to_available_agent(
                db,
                conversation=conversation,
                service_team_id=service_team_id,
                assigned_by_person_id=actor_person_id,
                reason=reason,
            )
        else:
            result = team_inbox_assignment.queue_conversation_for_team(
                db,
                conversation=conversation,
                service_team_id=service_team_id,
                assigned_by_person_id=actor_person_id,
                reason=reason,
            )
        if result.kind in {"assigned", "queued"}:
            updated.append(str(conversation.id))
        else:
            skipped.append(
                {
                    "conversation_id": str(conversation.id),
                    "reason": result.reason or result.kind,
                }
            )
    db.flush()
    return {"updated": updated, "skipped": skipped}


def list_failed_outbound_messages(
    db: Session,
    *,
    limit: int = 100,
) -> list[InboxMessage]:
    return list(
        db.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .filter(InboxMessage.metadata_["delivery_status"].as_string() == "failed")
        .order_by(InboxMessage.created_at.desc())
        .limit(limit)
        .all()
    )


def retry_failed_outbound_batch(
    db: Session,
    *,
    limit: int = 50,
    max_retry_count: int = 5,
) -> dict[str, object]:
    retried: list[str] = []
    skipped: list[dict[str, str]] = []
    for message in list_failed_outbound_messages(db, limit=max(1, int(limit))):
        metadata = dict(message.metadata_ or {})
        retry_count = int(metadata.get("retry_count") or 0)
        if retry_count >= max_retry_count:
            skipped.append({"message_id": str(message.id), "reason": "max_retries"})
            continue
        result = team_inbox_outbound.retry_outbound_message(db, message=message)
        if result.kind in {"sent", "queued"}:
            retried.append(str(message.id))
        else:
            skipped.append(
                {
                    "message_id": str(message.id),
                    "reason": result.reason or result.kind,
                }
            )
    db.flush()
    return {"retried": retried, "skipped": skipped}


def queue_metrics(db: Session) -> InboxQueueMetrics:
    from app.services import team_inbox_read

    open_rows = (
        db.query(InboxConversation)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status != "resolved")
        .all()
    )
    open_ids = [row.id for row in open_rows]
    active_assignment_ids = (
        {
            row[0]
            for row in db.query(InboxConversationAssignment.conversation_id)
            .filter(InboxConversationAssignment.conversation_id.in_(open_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        if open_ids
        else set()
    )
    return InboxQueueMetrics(
        total_open=len(open_rows),
        needs_response=team_inbox_read.list_conversations(
            db, needs_response=True, limit=1
        ).count,
        failed_outbound=int(
            db.query(func.count(InboxMessage.id))
            .filter(InboxMessage.direction == "outbound")
            .filter(InboxMessage.metadata_["delivery_status"].as_string() == "failed")
            .scalar()
            or 0
        ),
        unassigned_open=sum(
            1
            for conversation in open_rows
            if conversation.id not in active_assignment_ids
        ),
        muted_open=sum(1 for conversation in open_rows if conversation.is_muted),
        snoozed_open=sum(
            1 for conversation in open_rows if conversation.snoozed_until is not None
        ),
    )


def set_satisfaction(
    db: Session,
    *,
    conversation: InboxConversation,
    rating: int,
    comment: str | None = None,
    actor: str | None = None,
) -> InboxConversation:
    clean_rating = int(rating)
    if clean_rating < 1 or clean_rating > 5:
        raise InboxOperationError("Rating must be between 1 and 5.")
    if conversation.status != "resolved":
        raise InboxOperationError("Only resolved conversations can be rated.")
    metadata = dict(conversation.metadata_ or {})
    metadata["csat"] = {
        "rating": clean_rating,
        "comment": str(comment or "").strip() or None,
        "actor": str(actor or "").strip() or None,
        "rated_at": datetime.now(UTC).isoformat(),
    }
    conversation.metadata_ = metadata
    db.flush()
    return conversation


def auto_resolve_stale_conversations(
    db: Session,
    *,
    stale_hours: int = 72,
    limit: int = 200,
    now: datetime | None = None,
) -> int:
    clock = now or datetime.now(UTC)
    cutoff = clock - timedelta(hours=max(1, int(stale_hours)))
    rows = (
        db.query(InboxConversation)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status.in_(["open", "pending", "snoozed"]))
        .filter(InboxConversation.last_message_at.isnot(None))
        .filter(InboxConversation.last_message_at <= cutoff)
        .order_by(InboxConversation.last_message_at.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    if not rows:
        return 0
    latest_messages = {
        message.conversation_id: message
        for message in db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_([row.id for row in rows]))
        .filter(InboxMessage.direction != "internal")
        .order_by(InboxMessage.created_at.asc())
        .all()
    }
    resolved = 0
    for conversation in rows:
        latest = latest_messages.get(conversation.id)
        if latest is not None and latest.direction == "inbound":
            continue
        metadata = dict(conversation.metadata_ or {})
        history = metadata.get("status_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "from": conversation.status,
                "to": "resolved",
                "at": clock.isoformat(),
                "source": "team_inbox_auto_resolve",
            }
        )
        metadata["status_history"] = history[-50:]
        metadata["auto_resolved_at"] = clock.isoformat()
        conversation.status = "resolved"
        conversation.metadata_ = metadata
        resolved += 1
    db.flush()
    return resolved


SNOOZE_UNTIL_REPLY_KEY = "snooze_until_reply"


def snooze_until_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    actor_person_id: str | UUID | None = None,
) -> InboxConversation:
    """Snooze a conversation with no wake time — the customer's reply wakes it.

    Stored as a metadata flag rather than a far-future ``snoozed_until``, so the
    queue's snoozed filter still means "asleep" while nothing invents a wake
    date the operator never chose. ``wake_on_inbound`` clears it.
    """
    metadata = dict(conversation.metadata_ or {})
    metadata[SNOOZE_UNTIL_REPLY_KEY] = True
    history = metadata.get("workflow_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "at": _now_iso(),
            "actor_id": str(coerce_uuid(actor_person_id))
            if coerce_uuid(actor_person_id)
            else None,
            "source": "team_inbox_workflow",
            "snooze_until_reply": True,
        }
    )
    metadata["workflow_history"] = history[-50:]
    conversation.metadata_ = metadata
    conversation.snoozed_until = None
    conversation.status = "snoozed"
    db.flush()
    return conversation


def wake_on_inbound(db: Session, *, conversation: InboxConversation) -> bool:
    """Wake a conversation that was snoozed until the customer replied.

    Called from every inbound path. Returns whether it woke anything, so a
    caller can tell the difference between "nothing to do" and a state change.

    Deliberately narrow: it only acts on the until-reply flag. A conversation
    snoozed to a *time* keeps sleeping, because the operator picked that time
    knowing the customer might write again; and a resolved conversation is left
    alone so an inbound message does not silently reopen closed work.
    """
    metadata = dict(conversation.metadata_ or {})
    if not metadata.pop(SNOOZE_UNTIL_REPLY_KEY, False):
        return False

    history = metadata.get("workflow_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "at": _now_iso(),
            "actor_id": None,
            "source": "team_inbox_inbound",
            "woke_on_reply": True,
        }
    )
    metadata["workflow_history"] = history[-50:]
    conversation.metadata_ = metadata
    conversation.snoozed_until = None
    if conversation.status == "snoozed":
        conversation.status = "open"
    db.flush()
    return True


def render_conversation_transcript(
    db: Session, *, conversation: InboxConversation
) -> tuple[str, str]:
    """Render a conversation as (subject, html) for emailing.

    Internal notes and comments are deliberately excluded: a transcript is
    often forwarded to the customer or a third party, and internal
    collaboration is not theirs to read. Only the messages that were actually
    exchanged appear.
    """
    from html import escape as _escape

    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction.in_(["inbound", "outbound"]))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )

    subject = (
        f"Transcript: {conversation.subject}"
        if conversation.subject
        else f"Transcript: conversation with {conversation.contact_address or 'customer'}"
    )

    rows = []
    for message in messages:
        metadata = message.metadata_ or {}
        if metadata.get("delivery_status") == SCHEDULED_DELIVERY_STATUS_FOR_TRANSCRIPT:
            # Not sent yet — it is not part of what was exchanged.
            continue
        who = "Us" if message.direction == "outbound" else "Customer"
        when = (message.sent_at or message.created_at).strftime("%Y-%m-%d %H:%M UTC")
        body = _escape(str(message.body or "")).replace("\n", "<br>")
        rows.append(
            f"<p style='margin:0 0 12px'><strong>{who}</strong> "
            f"<span style='color:#64748b'>{when}</span><br>{body}</p>"
        )

    if not rows:
        rows.append("<p><em>No messages were exchanged.</em></p>")

    html = f"<h2 style='margin:0 0 16px'>{_escape(subject)}</h2>" + "".join(rows)
    return subject, html


# Kept as a module constant so the transcript filter cannot drift from the
# outbound owner's scheduled marker.
SCHEDULED_DELIVERY_STATUS_FOR_TRANSCRIPT = "scheduled"
