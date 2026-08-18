"""Service helpers for admin notification web routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationStatus,
)
from app.schemas.notification import (
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
)
from app.services import email as email_service
from app.services import notification as notification_service
from app.services import notification_template_conditions as condition_service
from app.services import notification_template_renderer as template_renderer
from app.services import settings_spec
from app.services import sms as sms_service
from app.services.integrations import whatsapp_capability
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
)
from app.services.whatsapp_notification_templates import (
    provider_template_from_template,
    sync_whatsapp_registry_templates,
)


@dataclass(frozen=True, slots=True)
class NotificationQueuePresentation:
    label: str
    tone: str
    timing: str


def notification_queue_presentation(
    notification: object,
    *,
    now: datetime | None = None,
) -> NotificationQueuePresentation:
    timestamp = now or datetime.now(UTC)
    status = getattr(notification, "status", NotificationStatus.queued)
    send_at = getattr(notification, "send_at", None)
    if send_at is not None and send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=UTC)
    if (
        status is NotificationStatus.queued
        and send_at is not None
        and send_at > timestamp
    ):
        return NotificationQueuePresentation(
            label="Scheduled",
            tone="neutral",
            timing="scheduled",
        )
    presentations = {
        NotificationStatus.queued: ("Queued and due", "warning"),
        NotificationStatus.sending: ("Sending", "info"),
        NotificationStatus.delivered: ("Delivered", "active"),
        NotificationStatus.failed: ("Retrying" if send_at else "Failed", "error"),
        NotificationStatus.canceled: ("Canceled", "neutral"),
    }
    label, tone = presentations[status]
    return NotificationQueuePresentation(
        label=label,
        tone=tone,
        timing="retry" if status is NotificationStatus.failed and send_at else "due",
    )


def channels() -> list[str]:
    return [item.value for item in NotificationChannel]


def notification_statuses() -> list[str]:
    return [item.value for item in NotificationStatus]


def delivery_statuses() -> list[str]:
    return [item.value for item in DeliveryStatus]


# UI page contracts for the admin notification lists. Each is the
# projection-boundary owner for its surface: it declares searchable/filterable
# /sortable fields, default order and page sizes. The routes validate and submit
# through the contract; the context functions remain the read owners that issue
# the SQL via notification_service.
NOTIFICATION_TEMPLATES_LIST_DEFINITION = ListDefinition(
    key="notification_templates",
    fields=(
        ListFieldDefinition("search", "Search", searchable=True),
        ListFieldDefinition("channel", "Channel", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("name", "Name", sortable=True),
    ),
    default_sort="name",
    default_sort_dir="asc",
    default_per_page=25,
)

NOTIFICATION_QUEUE_LIST_DEFINITION = ListDefinition(
    key="notification_queue",
    fields=(
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("channel", "Channel", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
    default_per_page=25,
)

NOTIFICATION_HISTORY_LIST_DEFINITION = ListDefinition(
    key="notification_history",
    fields=(
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("occurred_at", "Occurred", sortable=True),
    ),
    default_sort="occurred_at",
    default_sort_dir="desc",
    default_per_page=25,
)


def build_templates_list_query(
    *,
    channel: str | None = None,
    status: str | None = None,
    search: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise notification-template list params through the page contract."""
    return NOTIFICATION_TEMPLATES_LIST_DEFINITION.build_query(
        search=search,
        filters={"channel": channel, "status": status},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def build_queue_list_query(
    *,
    status: str | None = None,
    channel: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise notification-queue list params through the page contract."""
    return NOTIFICATION_QUEUE_LIST_DEFINITION.build_query(
        search=None,
        filters={"status": status, "channel": channel},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def build_history_list_query(
    *,
    status: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise notification-history list params through the page contract."""
    return NOTIFICATION_HISTORY_LIST_DEFINITION.build_query(
        search=None,
        filters={"status": status},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def templates_list_context(db: Session, query: ListQuery) -> dict[str, object]:
    """Read owner for the notification-template list (reads a validated query)."""
    sync_whatsapp_registry_templates(db)
    channel = query.filter_value("channel")
    status = query.filter_value("status")
    search = query.search
    page = query.page
    per_page = query.per_page
    offset = query.offset
    effective_channel = channel if channel else None
    effective_status = str(status or "").strip().lower()
    is_active = (
        True
        if effective_status == "active"
        else False
        if effective_status == "inactive"
        else None
    )
    effective_search = str(search or "").strip() or None
    template_list = notification_service.templates.list(
        db=db,
        channel=effective_channel,
        is_active=is_active,
        search=effective_search,
        order_by="name",
        order_dir=query.sort_dir,
        limit=per_page,
        offset=offset,
    )
    total = notification_service.templates.count(
        db=db,
        channel=effective_channel,
        is_active=is_active,
        search=effective_search,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "templates": template_list,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "channel": channel,
        "status": effective_status,
        "search": effective_search or "",
        "channel_counts": notification_service.templates.channel_counts(db),
        "channels": channels(),
    }


def template_form_context(
    db: Session,
    *,
    template_id: UUID | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    sync_whatsapp_registry_templates(db)
    template = None
    if template_id is not None:
        template = notification_service.templates.get(
            db=db, template_id=str(template_id)
        )
        if not template:
            return None

    is_edit = template_id is not None
    context: dict[str, object] = {
        "channels": channels(),
        "action_url": f"/admin/notifications/templates/{template_id}"
        if is_edit
        else "/admin/notifications/templates",
        "form_title": "Edit Notification Template"
        if is_edit
        else "New Notification Template",
        "submit_label": "Update Template" if is_edit else "Create Template",
        "template_variables": template_renderer.TEMPLATE_VARIABLES,
        "condition_fields": condition_service.CONDITION_FIELD_HELP,
    }
    if template is not None:
        context["template"] = template
        context["conditions_json"] = _conditions_to_json(template.conditions)
    if error:
        context["error"] = error
    return context


def _normalize_template_code(code: str) -> str:
    return code.strip().lower().replace(" ", "_")


def create_template(
    db: Session,
    *,
    name: str,
    code: str,
    channel: str,
    subject: str | None,
    body: str,
    conditions_json: str | None = None,
):
    normalized_code = _normalize_template_code(code)
    template_renderer.validate_template_text(subject, body, code=normalized_code)
    conditions = _parse_conditions_json(conditions_json)
    payload = NotificationTemplateCreate(
        name=name.strip(),
        code=normalized_code,
        channel=NotificationChannel(channel),
        subject=subject.strip() if subject else None,
        body=body.strip(),
        conditions=conditions,
    )
    return notification_service.templates.create(db=db, payload=payload)


def update_template(
    db: Session,
    *,
    template_id: UUID,
    name: str,
    code: str,
    channel: str,
    subject: str | None,
    body: str,
    is_active: bool,
    conditions_json: str | None = None,
):
    normalized_code = _normalize_template_code(code)
    template_renderer.validate_template_text(subject, body, code=normalized_code)
    conditions = _parse_conditions_json(conditions_json)
    payload = NotificationTemplateUpdate(
        name=name.strip(),
        code=normalized_code,
        channel=NotificationChannel(channel),
        subject=subject.strip() if subject else None,
        body=body.strip(),
        conditions=conditions,
        is_active=is_active,
    )
    return notification_service.templates.update(
        db=db, template_id=str(template_id), payload=payload
    )


def _parse_conditions_json(raw: str | None) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Conditions must be valid JSON: {exc.msg}") from exc
    return condition_service.validate_conditions(parsed)


def _conditions_to_json(conditions: object) -> str:
    try:
        normalized = condition_service.validate_conditions(conditions)
    except condition_service.NotificationTemplateConditionError:
        return json.dumps(conditions or {}, indent=2, sort_keys=True)
    if not normalized:
        return ""
    return json.dumps(normalized, indent=2, sort_keys=True)


def delete_template(db: Session, *, template_id: UUID) -> None:
    notification_service.templates.delete(db=db, template_id=str(template_id))


def toggle_template(db: Session, *, template_id: UUID):
    template = notification_service.templates.get(db=db, template_id=str(template_id))
    return notification_service.templates.update(
        db=db,
        template_id=str(template_id),
        payload=NotificationTemplateUpdate(is_active=not template.is_active),
    )


def preview_variables(test_variables_json: str | None) -> dict[str, str]:
    variables = template_renderer.default_preview_variables()
    if not test_variables_json or not test_variables_json.strip():
        return variables

    parsed = json.loads(test_variables_json)
    if not isinstance(parsed, dict):
        raise ValueError("test_variables_json must be a JSON object")
    for key, value in parsed.items():
        variables[str(key)] = "" if value is None else str(value)
    return variables


def render_template_preview(
    db: Session,
    *,
    template_id: UUID,
    test_variables_json: str | None,
) -> dict[str, object]:
    template = notification_service.templates.get(db=db, template_id=str(template_id))
    variables = preview_variables(test_variables_json)
    return {
        "rendered_subject": template_renderer.render_template_text(
            template.subject or "",
            variables,
        ),
        "rendered_body": template_renderer.render_template_text(
            template.body, variables
        ),
        "variables": variables,
        "channel": template.channel.value,
    }


def render_template_preview_from_fields(
    *,
    channel: str,
    subject: str | None,
    body: str,
    test_variables_json: str | None,
) -> dict[str, object]:
    variables = preview_variables(test_variables_json)
    return {
        "rendered_subject": template_renderer.render_template_text(
            subject or "",
            variables,
        ),
        "rendered_body": template_renderer.render_template_text(body, variables),
        "variables": variables,
        "channel": channel,
    }


def send_template_test(
    db: Session,
    *,
    template_id: UUID,
    test_recipient: str,
    test_variables_json: str | None,
) -> str:
    from app.services import email as email_service
    from app.services import sms as sms_service
    from app.services.integrations import whatsapp_capability as whatsapp_service

    template = notification_service.templates.get(db=db, template_id=str(template_id))
    variables = preview_variables(test_variables_json)
    rendered_subject = template_renderer.render_template_text(
        template.subject or "Test Notification",
        variables,
    )
    rendered_body = template_renderer.render_template_text(template.body, variables)
    recipient = test_recipient.strip()

    if template.channel == NotificationChannel.sms:
        sms_service.send_sms(db=db, to_phone=recipient, body=rendered_body, track=True)
        return f"Test SMS sent to {test_recipient}"
    if template.channel == NotificationChannel.email:
        email_service.send_email(
            db=db,
            to_email=recipient,
            subject=rendered_subject,
            body_html=rendered_body,
            activity="notification_test",
        )
        return f"Test email sent to {test_recipient}"
    if template.channel == NotificationChannel.whatsapp:
        provider_template = provider_template_from_template(template)
        result = whatsapp_service.send_template_message(
            db=db,
            recipient=recipient,
            template_name=str((provider_template or {}).get("name") or template.code),
            language=str((provider_template or {}).get("language") or "") or None,
            dry_run=False,
        )
        if not result.get("ok"):
            raise RuntimeError(
                str(result.get("response") or "Failed to send WhatsApp test")
            )
        return f"Test WhatsApp message sent to {test_recipient}"

    return f"Test notification queued for {template.channel.value}"


def _email_channel_ready(db: Session) -> tuple[bool, str]:
    senders = email_service.list_smtp_senders(db)
    if senders:
        return True, "SMTP sender profiles configured"
    return False, "No SMTP sender profile configured"


def _sms_channel_ready(db: Session) -> tuple[bool, str]:
    # Mirrors sms.send_sms exactly, and fails closed for the same reason: an
    # unconfigured channel must report "not ready", never "ready".
    if not settings_spec.resolve_boolean(db, SettingDomain.notification, "sms_enabled"):
        return False, "SMS is disabled"

    provider = settings_spec.resolve_string(
        db, SettingDomain.notification, "sms_provider"
    )
    if not provider:
        return False, "No SMS provider configured"
    if provider == "twilio":
        account_sid, auth_token = sms_service._sms_credentials()
        from_number = settings_spec.resolve_string(
            db, SettingDomain.notification, "sms_from_number"
        )
        if account_sid and auth_token and from_number:
            return True, "Twilio credentials configured"
        return False, "Twilio credentials are incomplete"
    if provider == "africastalking":
        api_key, _ = sms_service._sms_credentials()
        if api_key:
            return True, "Africa's Talking API key configured"
        return False, "Africa's Talking API key is missing"
    if provider == "webhook":
        webhook_url = settings_spec.resolve_string(
            db, SettingDomain.notification, "sms_webhook_url"
        )
        if webhook_url:
            return True, "Webhook endpoint configured"
        return False, "Webhook URL is missing"
    return False, "SMS provider is not recognized"


def _whatsapp_channel_ready(db: Session) -> tuple[bool, str]:
    try:
        config = whatsapp_capability.active_config(db)
    except Exception:
        return False, "No enabled WhatsApp capability binding"
    if not str(config.get("phone_number") or "").strip():
        return False, "WhatsApp phone number is missing"
    return True, "Meta Cloud API is configured"


def bulk_notification_setup_context(db: Session) -> dict[str, object]:
    sync_whatsapp_registry_templates(db)
    template_list = notification_service.templates.list(
        db=db,
        channel=None,
        is_active=True,
        search=None,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    channel_checks = {
        NotificationChannel.email.value: _email_channel_ready(db),
        NotificationChannel.sms.value: _sms_channel_ready(db),
        NotificationChannel.whatsapp.value: _whatsapp_channel_ready(db),
    }
    channels_state = [
        {
            "id": channel.value,
            "label": "WhatsApp"
            if channel == NotificationChannel.whatsapp
            else channel.value.capitalize(),
            "enabled": channel.value
            in {
                NotificationChannel.email.value,
                NotificationChannel.sms.value,
                NotificationChannel.whatsapp.value,
            },
            "ready": channel_checks.get(channel.value, (False, "Unsupported"))[0],
            "message": channel_checks.get(channel.value, (False, "Unsupported"))[1],
            "template_count": sum(
                1 for template in template_list if template.channel == channel
            ),
            "settings_url": (
                "/admin/system/email"
                if channel == NotificationChannel.email
                else "/admin/integrations/whatsapp/config"
                if channel == NotificationChannel.whatsapp
                else None
            ),
        }
        for channel in (
            NotificationChannel.email,
            NotificationChannel.sms,
            NotificationChannel.whatsapp,
        )
    ]
    templates_state = []
    for template in template_list:
        provider_template = provider_template_from_template(template)
        language = str((provider_template or {}).get("language") or "")
        templates_state.append(
            {
                "id": str(template.id),
                "name": template.name,
                "code": template.code,
                "language": language,
                "label": f"{template.name} ({language})" if language else template.name,
                "channel": template.channel.value,
                "subject": template.subject or "",
                "is_active": bool(template.is_active),
                "is_registry_template": bool(provider_template),
                "provider_template_name": (provider_template or {}).get("name") or "",
            }
        )
    return {
        "bulk_notification_channels": channels_state,
        "bulk_notification_templates": templates_state,
    }


def queue_context(db: Session, query: ListQuery) -> dict[str, object]:
    """Read owner for the notification-queue list (reads a validated query)."""
    status = query.filter_value("status")
    channel = query.filter_value("channel")
    page = query.page
    per_page = query.per_page
    offset = query.offset
    notification_status = status if status else "queued"
    effective_channel = channel if channel else None
    notifications_list = notification_service.notifications.list(
        db=db,
        channel=effective_channel,
        status=notification_status,
        is_active=True,
        order_by="created_at",
        order_dir=query.sort_dir,
        limit=per_page,
        offset=offset,
    )
    total = notification_service.notifications.count(
        db=db,
        channel=effective_channel,
        status=notification_status,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    presentations = {
        str(notification.id): notification_queue_presentation(notification)
        for notification in notifications_list
    }
    return {
        "notifications": notifications_list,
        "notification_presentations": presentations,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": notification_status,
        "channel": channel,
        "status_counts": notification_service.notifications.status_counts(db),
        "channels": channels(),
        "statuses": notification_statuses(),
    }


def history_context(db: Session, query: ListQuery) -> dict[str, object]:
    """Read owner for the notification-history list (reads a validated query)."""
    status = query.filter_value("status")
    page = query.page
    per_page = query.per_page
    offset = query.offset
    deliveries = notification_service.deliveries.list(
        db=db,
        notification_id=None,
        status=status if status else None,
        is_active=True,
        order_by="occurred_at",
        order_dir=query.sort_dir,
        limit=per_page,
        offset=offset,
    )
    total = notification_service.deliveries.count(
        db=db,
        status=status if status else None,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "deliveries": deliveries,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "statuses": delivery_statuses(),
    }
