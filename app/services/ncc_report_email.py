"""Typed owner for scheduled NCC complaints-workbook delivery.

The scheduler is only a five-minute trigger. This owner resolves the effective
Tuesday schedule, arbitrates one occurrence per local date, preserves the exact
XLSX artifact, and stages one durable communication intent atomically.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from html import escape
from string import Formatter
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.ncc_reporting import NccWeeklyReportRun, NccWeeklyReportRunStatus
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import ncc_complaints_report, ncc_workbook
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.branding_config import get_brand
from app.services.communication_intents import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import (
    submit as submit_communication_intent,
)
from app.services.domain_errors import DomainError
from app.services.domain_settings import notification_settings
from app.services.email import resolve_recipient_addresses
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    execute_owner_savepoint,
)
from app.services.settings_spec import resolve_value

OWNER = "communications.ncc_weekly_delivery"
CONFIGURATION_CONCERN = "NCC weekly delivery configuration"
OCCURRENCE_CONCERN = "NCC weekly report occurrence and artifact"
SCHEDULE_KEY = "ncc_complaints"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ENABLED_KEY = "ncc_report_email_enabled"
TO_KEY = "ncc_report_email_to"
CC_KEY = "ncc_report_email_cc"
BCC_KEY = "ncc_report_email_bcc"
SENDER_KEY = "ncc_report_email_sender_key"
SUBJECT_KEY = "ncc_report_email_subject"
BODY_TEMPLATE_KEY = "ncc_report_email_body_template"
LOCAL_TIME_KEY = "ncc_report_email_local_time"
TIMEZONE_KEY = "ncc_report_email_timezone"
SEND_DAY_KEY = "ncc_report_email_send_day"
LOOKBACK_KEY = "ncc_report_email_lookback_days"

DEFAULT_SUBJECT = "Weekly NCC Report"
DEFAULT_BODY_TEMPLATE = (
    "Please find attached the NCC complaints report for the last "
    "{lookback_days} day(s).\nRows included: {row_count}.\n"
    "Rows not yet filable: {not_filable_count}.\n"
    "Download: {download_url}"
)
DEFAULT_LOCAL_TIME = "08:00"
DEFAULT_TIMEZONE = "Africa/Lagos"
DEFAULT_SEND_DAY = "tuesday"
DEFAULT_LOOKBACK_DAYS = 7

_SENDER_KEY_RE = re.compile(r"^[a-z0-9_-]{1,80}$")
_BODY_TEMPLATE_FIELDS = frozenset(
    {
        "download_url",
        "lookback_days",
        "not_filable_count",
        "report_date",
        "row_count",
    }
)


class NccWeekday(StrEnum):
    monday = "monday"
    tuesday = "tuesday"
    wednesday = "wednesday"
    thursday = "thursday"
    friday = "friday"
    saturday = "saturday"
    sunday = "sunday"

    @property
    def python_weekday(self) -> int:
        return tuple(NccWeekday).index(self)


class NccWeeklyRunDecision(StrEnum):
    disabled = "disabled"
    missing_recipient = "missing_recipient"
    not_scheduled_day = "not_scheduled_day"
    before_scheduled_time = "before_scheduled_time"
    already_queued = "already_queued"
    queued = "queued"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class NccWeeklyRecipientSet:
    to: str | None
    cc: tuple[str, ...]
    bcc: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NccWeeklyDeliveryConfiguration:
    enabled: bool
    recipients: NccWeeklyRecipientSet
    sender_key: str | None
    subject: str
    body_template: str
    local_time: time
    timezone: str
    send_day: NccWeekday
    lookback_days: int


@dataclass(frozen=True, slots=True)
class UpdateNccWeeklyDeliveryConfigurationCommand:
    context: CommandContext
    enabled: bool
    to_address: str
    cc_addresses: str
    bcc_addresses: str
    sender_key: str
    subject: str
    body_template: str
    local_time: str
    timezone: str
    send_day: str
    lookback_days: int


@dataclass(frozen=True, slots=True)
class NccWeeklyDeliveryConfigurationOutcome:
    configuration: NccWeeklyDeliveryConfiguration


@dataclass(frozen=True, slots=True)
class NccWeeklyDeliveryConfigurationPreview:
    """Validated values used by both the owner and dry-run migration tooling."""

    recipients: NccWeeklyRecipientSet
    sender_key: str | None
    subject: str
    body_template: str
    local_time: time
    timezone: str
    send_day: NccWeekday
    lookback_days: int


@dataclass(frozen=True, slots=True)
class RunNccWeeklyDeliveryCommand:
    context: CommandContext
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class NccWeeklyDeliveryOutcome:
    decision: NccWeeklyRunDecision
    scheduled_local_date: str | None = None
    run_id: UUID | None = None
    notification_id: UUID | None = None
    row_count: int = 0
    not_filable_count: int = 0
    failure_code: str | None = None

    @property
    def queued(self) -> bool:
        return self.decision is NccWeeklyRunDecision.queued


@dataclass(frozen=True, slots=True)
class NccWeeklyRunView:
    run_id: UUID
    scheduled_local_date: str
    status: NccWeeklyReportRunStatus
    row_count: int
    not_filable_count: int
    notification_id: UUID | None
    delivery_status: NotificationStatus | None
    failure_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NccWeeklyRunHistoryQuery:
    limit: int = 10


@dataclass(frozen=True, slots=True)
class NccWeeklyArtifactQuery:
    run_id: UUID


@dataclass(frozen=True, slots=True)
class NccWeeklyArtifact:
    filename: str
    content_type: str
    content: bytes
    sha256: str


class NccWeeklyDeliveryError(DomainError):
    """Transport-neutral configuration or occurrence error."""


_CONFIG_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONFIGURATION_CONCERN,
    name="update_ncc_weekly_delivery_configuration",
)
_RUN_DEFINITION = OwnerCommandDefinition(
    owner=OWNER,
    concern=OCCURRENCE_CONCERN,
    name="run_ncc_weekly_delivery",
)


def _error(
    suffix: str, message: str, *, field: str | None = None
) -> NccWeeklyDeliveryError:
    details: dict[str, object] = {}
    if field is not None:
        details["field"] = field
    return NccWeeklyDeliveryError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _text_value(db: Session, key: str, default: str = "") -> str:
    value = resolve_value(db, SettingDomain.notification, key)
    return str(value if value is not None else default).strip()


def _bool_value(db: Session, key: str, default: bool = False) -> bool:
    value = resolve_value(db, SettingDomain.notification, key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _integer_value(db: Session, key: str, default: int) -> int:
    value = resolve_value(db, SettingDomain.notification, key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _parse_local_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise _error(
            "invalid_configuration",
            "Send time must use the HH:MM 24-hour format.",
            field="local_time",
        ) from exc
    return parsed.time().replace(second=0, microsecond=0)


def _parse_timezone(value: str) -> str:
    normalized = value.strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(normalized)
    except Exception as exc:
        raise _error(
            "invalid_configuration",
            "The selected timezone is not available.",
            field="timezone",
        ) from exc
    return normalized


def _parse_weekday(value: str) -> NccWeekday:
    try:
        return NccWeekday(value.strip().lower())
    except ValueError as exc:
        raise _error(
            "invalid_configuration",
            "Select a valid report weekday.",
            field="send_day",
        ) from exc


def _parse_recipient_set(
    *, to_address: str, cc_addresses: str, bcc_addresses: str, require_to: bool
) -> NccWeeklyRecipientSet:
    primary = resolve_recipient_addresses(to_address)
    cc = resolve_recipient_addresses(cc_addresses)
    bcc = resolve_recipient_addresses(bcc_addresses)
    if primary.rejected or len(primary.deliverable) > 1:
        raise _error(
            "invalid_configuration",
            "Enter exactly one valid primary recipient.",
            field="to_address",
        )
    if require_to and not primary.deliverable:
        raise _error(
            "invalid_configuration",
            "A primary recipient is required when weekly delivery is enabled.",
            field="to_address",
        )
    if cc.rejected:
        raise _error(
            "invalid_configuration",
            "One or more CC recipients are invalid.",
            field="cc_addresses",
        )
    if bcc.rejected:
        raise _error(
            "invalid_configuration",
            "One or more BCC recipients are invalid.",
            field="bcc_addresses",
        )
    return NccWeeklyRecipientSet(
        to=primary.deliverable[0] if primary.deliverable else None,
        cc=cc.deliverable,
        bcc=bcc.deliverable,
    )


def preview_configuration(
    command: UpdateNccWeeklyDeliveryConfigurationCommand,
) -> NccWeeklyDeliveryConfigurationPreview:
    """Validate a complete configuration without opening a transaction."""

    recipients = _parse_recipient_set(
        to_address=command.to_address,
        cc_addresses=command.cc_addresses,
        bcc_addresses=command.bcc_addresses,
        require_to=command.enabled,
    )
    local_time = _parse_local_time(command.local_time)
    timezone = _parse_timezone(command.timezone)
    send_day = _parse_weekday(command.send_day)
    if not 1 <= command.lookback_days <= 366:
        raise _error(
            "invalid_configuration",
            "Lookback days must be between 1 and 366.",
            field="lookback_days",
        )
    sender_key = command.sender_key.strip().lower() or None
    if sender_key is not None and not _SENDER_KEY_RE.fullmatch(sender_key):
        raise _error(
            "invalid_configuration",
            "The SMTP sender key may contain lowercase letters, numbers, "
            "underscores and hyphens only.",
            field="sender_key",
        )
    subject = command.subject.strip() or DEFAULT_SUBJECT
    if len(subject) > 200:
        raise _error(
            "invalid_configuration",
            "The email subject cannot exceed 200 characters.",
            field="subject",
        )
    body_template = command.body_template.strip() or DEFAULT_BODY_TEMPLATE
    try:
        parsed_fields = tuple(Formatter().parse(body_template))
    except ValueError as exc:
        raise _error(
            "invalid_configuration",
            "The email body template contains invalid braces.",
            field="body_template",
        ) from exc
    fields: set[str] = set()
    for _literal, field_name, format_spec, conversion in parsed_fields:
        if field_name is None:
            continue
        if format_spec or conversion is not None:
            raise _error(
                "invalid_configuration",
                "Email body placeholders cannot use formatting or conversion.",
                field="body_template",
            )
        fields.add(field_name)
    if not fields <= _BODY_TEMPLATE_FIELDS:
        raise _error(
            "invalid_configuration",
            "The email body template contains an unsupported placeholder.",
            field="body_template",
        )
    return NccWeeklyDeliveryConfigurationPreview(
        recipients=recipients,
        sender_key=sender_key,
        subject=subject,
        body_template=body_template,
        local_time=local_time,
        timezone=timezone,
        send_day=send_day,
        lookback_days=command.lookback_days,
    )


def get_configuration(db: Session) -> NccWeeklyDeliveryConfiguration:
    """Return the effective typed configuration."""

    enabled = _bool_value(db, ENABLED_KEY)
    recipients = _parse_recipient_set(
        to_address=_text_value(db, TO_KEY),
        cc_addresses=_text_value(db, CC_KEY),
        bcc_addresses=_text_value(db, BCC_KEY),
        require_to=False,
    )
    sender_key = _text_value(db, SENDER_KEY) or None
    if sender_key is not None and not _SENDER_KEY_RE.fullmatch(sender_key):
        raise _error(
            "invalid_configuration",
            "The SMTP sender key is invalid.",
            field="sender_key",
        )
    lookback_days = _integer_value(db, LOOKBACK_KEY, DEFAULT_LOOKBACK_DAYS)
    if not 1 <= lookback_days <= 366:
        raise _error(
            "invalid_configuration",
            "Lookback days must be between 1 and 366.",
            field="lookback_days",
        )
    return NccWeeklyDeliveryConfiguration(
        enabled=enabled,
        recipients=recipients,
        sender_key=sender_key,
        subject=_text_value(db, SUBJECT_KEY, DEFAULT_SUBJECT) or DEFAULT_SUBJECT,
        body_template=(
            _text_value(db, BODY_TEMPLATE_KEY, DEFAULT_BODY_TEMPLATE)
            or DEFAULT_BODY_TEMPLATE
        ),
        local_time=_parse_local_time(
            _text_value(db, LOCAL_TIME_KEY, DEFAULT_LOCAL_TIME) or DEFAULT_LOCAL_TIME
        ),
        timezone=_parse_timezone(
            _text_value(db, TIMEZONE_KEY, DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        ),
        send_day=_parse_weekday(
            _text_value(db, SEND_DAY_KEY, DEFAULT_SEND_DAY) or DEFAULT_SEND_DAY
        ),
        lookback_days=lookback_days,
    )


def is_enabled(db: Session) -> bool:
    return _bool_value(db, ENABLED_KEY)


def _stage_string(db: Session, key: str, value: str) -> None:
    notification_settings.stage_upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=value,
            is_active=True,
        ),
    )


def _stage_boolean(db: Session, key: str, value: bool) -> None:
    notification_settings.stage_upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            value_type=SettingValueType.boolean,
            value_text="true" if value else "false",
            value_json=None,
            is_active=True,
        ),
    )


def _stage_integer(db: Session, key: str, value: int) -> None:
    notification_settings.stage_upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            value_type=SettingValueType.integer,
            value_text=str(value),
            is_active=True,
        ),
    )


def update_configuration(
    db: Session, command: UpdateNccWeeklyDeliveryConfigurationCommand
) -> NccWeeklyDeliveryConfigurationOutcome:
    """Persist one complete, validated configuration through its owner."""

    def operation() -> NccWeeklyDeliveryConfigurationOutcome:
        preview = preview_configuration(command)

        _stage_boolean(db, ENABLED_KEY, command.enabled)
        _stage_string(db, TO_KEY, preview.recipients.to or "")
        _stage_string(db, CC_KEY, ", ".join(preview.recipients.cc))
        _stage_string(db, BCC_KEY, ", ".join(preview.recipients.bcc))
        _stage_string(db, SENDER_KEY, preview.sender_key or "")
        _stage_string(db, SUBJECT_KEY, preview.subject)
        _stage_string(db, BODY_TEMPLATE_KEY, preview.body_template)
        _stage_string(db, LOCAL_TIME_KEY, preview.local_time.strftime("%H:%M"))
        _stage_string(db, TIMEZONE_KEY, preview.timezone)
        _stage_string(db, SEND_DAY_KEY, preview.send_day.value)
        _stage_integer(db, LOOKBACK_KEY, preview.lookback_days)

        stage_audit_event(
            db,
            action="ncc.weekly_delivery_configuration_changed",
            entity_type="ncc_weekly_delivery_configuration",
            actor=AuditActor.system(command.context.actor),
            metadata={
                "owner": OWNER,
                "enabled": command.enabled,
                "send_day": preview.send_day.value,
                "local_time": preview.local_time.strftime("%H:%M"),
                "timezone": preview.timezone,
                "lookback_days": preview.lookback_days,
                "recipient_count": int(preview.recipients.to is not None)
                + len(preview.recipients.cc)
                + len(preview.recipients.bcc),
            },
        )
        emit_event(
            db,
            EventType.ncc_weekly_delivery_configuration_changed,
            {
                "enabled": command.enabled,
                "send_day": preview.send_day.value,
                "local_time": preview.local_time.strftime("%H:%M"),
                "timezone": preview.timezone,
            },
            actor=command.context.actor,
        )
        db.flush()
        return NccWeeklyDeliveryConfigurationOutcome(
            configuration=get_configuration(db)
        )

    return execute_owner_command(
        db,
        definition=_CONFIG_DEFINITION,
        context=command.context,
        operation=operation,
    )


def _configuration_fingerprint(config: NccWeeklyDeliveryConfiguration) -> str:
    evidence = {
        "enabled": config.enabled,
        "to": config.recipients.to,
        "cc": config.recipients.cc,
        "bcc": config.recipients.bcc,
        "sender_key": config.sender_key,
        "subject": config.subject,
        "body_template": config.body_template,
        "local_time": config.local_time.strftime("%H:%M"),
        "timezone": config.timezone,
        "send_day": config.send_day.value,
        "lookback_days": config.lookback_days,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class _TemplateValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _download_url(run_id: UUID) -> str:
    base_url = str(get_brand().get("app_url") or "").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise _error(
            "invalid_configuration",
            "The application URL must be configured before NCC delivery can run.",
            field="app_url",
        )
    return f"{base_url}/admin/reports/ncc-weekly-runs/{run_id}/download"


def _render_body(
    config: NccWeeklyDeliveryConfiguration,
    *,
    run_id: UUID,
    row_count: int,
    not_filable_count: int,
    report_date: str,
) -> tuple[str, str]:
    values = _TemplateValues(
        lookback_days=str(config.lookback_days),
        row_count=str(row_count),
        not_filable_count=str(not_filable_count),
        report_date=report_date,
        download_url=_download_url(run_id),
    )
    body_text = config.body_template.format_map(values)
    body_html = "<p>" + escape(body_text).replace("\n", "<br>") + "</p>"
    return body_text, body_html


def _local_observation(observed_at: datetime, timezone: str) -> datetime:
    normalized = observed_at
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(ZoneInfo(timezone))


def _existing_run(
    db: Session, *, scheduled_local_date: date
) -> NccWeeklyReportRun | None:
    return db.execute(
        select(NccWeeklyReportRun)
        .where(
            NccWeeklyReportRun.schedule_key == SCHEDULE_KEY,
            NccWeeklyReportRun.scheduled_local_date == scheduled_local_date,
        )
        .with_for_update()
    ).scalar_one_or_none()


def run_due_delivery(
    db: Session, command: RunNccWeeklyDeliveryCommand
) -> NccWeeklyDeliveryOutcome:
    """Queue the due Tuesday artifact exactly once, recording retry evidence."""

    def operation() -> NccWeeklyDeliveryOutcome:
        config = get_configuration(db)
        if not config.enabled:
            return NccWeeklyDeliveryOutcome(NccWeeklyRunDecision.disabled)
        if config.recipients.to is None:
            return NccWeeklyDeliveryOutcome(
                NccWeeklyRunDecision.missing_recipient,
                failure_code=f"{OWNER}.invalid_configuration",
            )

        local_now = _local_observation(command.observed_at, config.timezone)
        local_date = local_now.date()
        local_date_text = local_date.isoformat()
        if local_now.weekday() != config.send_day.python_weekday:
            return NccWeeklyDeliveryOutcome(
                NccWeeklyRunDecision.not_scheduled_day,
                scheduled_local_date=local_date_text,
            )
        if local_now.time().replace(second=0, microsecond=0) < config.local_time:
            return NccWeeklyDeliveryOutcome(
                NccWeeklyRunDecision.before_scheduled_time,
                scheduled_local_date=local_date_text,
            )

        existing = _existing_run(db, scheduled_local_date=local_date)
        if existing is not None and existing.status is NccWeeklyReportRunStatus.queued:
            return NccWeeklyDeliveryOutcome(
                NccWeeklyRunDecision.already_queued,
                scheduled_local_date=local_date_text,
                run_id=existing.id,
                notification_id=existing.notification_id,
                row_count=existing.row_count,
                not_filable_count=existing.not_filable_count,
            )

        scheduled_local = datetime.combine(
            local_date,
            config.local_time,
            tzinfo=ZoneInfo(config.timezone),
        )
        end = scheduled_local.astimezone(UTC)
        start = end - timedelta(days=config.lookback_days)
        run = existing or NccWeeklyReportRun(
            schedule_key=SCHEDULE_KEY,
            scheduled_local_date=local_date,
            schedule_timezone=config.timezone,
            scheduled_local_time=config.local_time.strftime("%H:%M"),
            window_start=start,
            window_end=end,
            configuration_fingerprint=_configuration_fingerprint(config),
            status=NccWeeklyReportRunStatus.failed,
            row_count=0,
            not_filable_count=0,
            failure_code="artifact_generation_pending",
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )
        if existing is None:
            db.add(run)
        else:
            run.schedule_timezone = config.timezone
            run.scheduled_local_time = config.local_time.strftime("%H:%M")
            run.window_start = start
            run.window_end = end
            run.configuration_fingerprint = _configuration_fingerprint(config)
            run.command_id = command.context.command_id
            run.correlation_id = command.context.correlation_id
            run.failure_code = "artifact_generation_pending"
            run.failure_detail = None
        db.flush()

        def stage_required_delivery() -> tuple[UUID, int, int]:
            snapshot = ncc_complaints_report.query_report(
                db=db,
                query=ncc_complaints_report.NccComplaintsReportQuery(
                    start=start, end=end
                ),
            )
            records = snapshot.record_mappings()
            rows = ncc_workbook.export_rows(records)
            workbook = ncc_workbook.build_workbook(rows, list(ncc_workbook.COLUMNS))
            if not workbook.startswith(b"PK\x03\x04"):
                raise _error(
                    "artifact_generation_failed",
                    "The generated NCC workbook is invalid.",
                )
            if len(workbook) > MAX_EMAIL_ATTACHMENT_BYTES:
                raise _error(
                    "artifact_generation_failed",
                    "The generated NCC workbook exceeds the email attachment limit.",
                )
            not_filable_count = sum(
                1
                for row in rows
                if not ncc_workbook.validation_status(row).startswith("[OK]")
            )
            filename = ncc_workbook.export_filename(local_now)
            artifact_sha256 = hashlib.sha256(workbook).hexdigest()
            run.artifact_filename = filename
            run.artifact_content_type = XLSX_CONTENT_TYPE
            run.artifact_content = workbook
            run.artifact_sha256 = artifact_sha256
            run.row_count = snapshot.total_complaints
            run.not_filable_count = not_filable_count
            run.failure_code = None
            run.failure_detail = None
            body_text, body_html = _render_body(
                config,
                run_id=run.id,
                row_count=snapshot.total_complaints,
                not_filable_count=not_filable_count,
                report_date=local_date_text,
            )
            intent = submit_communication_intent(
                db,
                CommunicationIntent(
                    subscriber_id=None,
                    event_type="ncc.weekly_report.ready",
                    category="regulatory",
                    subject=config.subject,
                    body=body_text,
                    communication_class=CommunicationClass.operational,
                    channels=(NotificationChannel.email,),
                    include_reseller=False,
                    recipients={NotificationChannel.email: config.recipients.to or ""},
                    audience_type="operational",
                    audience_id=run.id,
                    resolve_subscriber_identity=False,
                    metadata={
                        "body_html": body_html,
                        "body_text": body_text,
                        "sender_key": config.sender_key,
                        "activity": "ncc_report_email",
                        "cc": list(config.recipients.cc),
                        "bcc": list(config.recipients.bcc),
                        "ncc_weekly_report_run_id": str(run.id),
                    },
                    attachments=(
                        CommunicationAttachment(
                            kind=CommunicationAttachmentKind.ncc_weekly_xlsx,
                            entity_id=run.id,
                            filename=filename,
                            content_type=XLSX_CONTENT_TYPE,
                        ),
                    ),
                    dedupe_key=f"ncc-weekly:{local_date_text}",
                ),
            )
            if len(intent.queued) != 1:
                raise _error(
                    "delivery_intent_failed",
                    "The NCC workbook delivery could not be queued.",
                )
            notification = intent.queued[0]
            run.notification_id = notification.id
            run.status = NccWeeklyReportRunStatus.queued
            db.flush()
            return notification.id, snapshot.total_complaints, not_filable_count

        try:
            notification_id, row_count, not_filable_count = execute_owner_savepoint(
                db, stage_required_delivery
            )
        except Exception as exc:
            run.status = NccWeeklyReportRunStatus.failed
            run.failure_code = (
                exc.code
                if isinstance(exc, DomainError)
                else f"{OWNER}.artifact_or_delivery_failed"
            )
            run.failure_detail = type(exc).__name__
            run.artifact_filename = None
            run.artifact_content_type = None
            run.artifact_content = None
            run.artifact_sha256 = None
            run.notification_id = None
            run.row_count = 0
            run.not_filable_count = 0
            stage_audit_event(
                db,
                action="ncc.weekly_report_failed",
                entity_type="ncc_weekly_report_run",
                entity_id=str(run.id),
                actor=AuditActor.system(command.context.actor),
                is_success=False,
                metadata={"failure_code": run.failure_code},
            )
            db.flush()
            return NccWeeklyDeliveryOutcome(
                NccWeeklyRunDecision.failed,
                scheduled_local_date=local_date_text,
                run_id=run.id,
                failure_code=run.failure_code,
            )

        stage_audit_event(
            db,
            action="ncc.weekly_report_queued",
            entity_type="ncc_weekly_report_run",
            entity_id=str(run.id),
            actor=AuditActor.system(command.context.actor),
            metadata={
                "scheduled_local_date": local_date_text,
                "row_count": row_count,
                "not_filable_count": not_filable_count,
                "artifact_sha256": run.artifact_sha256,
            },
        )
        emit_event(
            db,
            EventType.ncc_weekly_report_queued,
            {
                "run_id": str(run.id),
                "notification_id": str(notification_id),
                "scheduled_local_date": local_date_text,
                "row_count": row_count,
                "not_filable_count": not_filable_count,
            },
            actor=command.context.actor,
        )
        db.flush()
        return NccWeeklyDeliveryOutcome(
            NccWeeklyRunDecision.queued,
            scheduled_local_date=local_date_text,
            run_id=run.id,
            notification_id=notification_id,
            row_count=row_count,
            not_filable_count=not_filable_count,
        )

    return execute_owner_command(
        db,
        definition=_RUN_DEFINITION,
        context=command.context,
        operation=operation,
    )


def list_recent_runs(
    db: Session, query: NccWeeklyRunHistoryQuery
) -> tuple[NccWeeklyRunView, ...]:
    rows = db.execute(
        select(NccWeeklyReportRun)
        .order_by(NccWeeklyReportRun.created_at.desc())
        .limit(max(1, min(query.limit, 50)))
    ).scalars()
    return tuple(
        NccWeeklyRunView(
            run_id=row.id,
            scheduled_local_date=row.scheduled_local_date.isoformat(),
            status=row.status,
            row_count=row.row_count,
            not_filable_count=row.not_filable_count,
            notification_id=row.notification_id,
            delivery_status=row.notification.status if row.notification else None,
            failure_code=row.failure_code,
            created_at=row.created_at,
        )
        for row in rows
    )


def get_artifact(db: Session, query: NccWeeklyArtifactQuery) -> NccWeeklyArtifact:
    run = db.get(NccWeeklyReportRun, query.run_id)
    if (
        run is None
        or run.status is not NccWeeklyReportRunStatus.queued
        or run.artifact_content is None
        or run.artifact_filename is None
        or run.artifact_content_type is None
        or run.artifact_sha256 is None
    ):
        raise _error("artifact_not_found", "The NCC report artifact is unavailable.")
    digest = hashlib.sha256(run.artifact_content).hexdigest()
    if digest != run.artifact_sha256:
        raise _error(
            "artifact_integrity_failed",
            "The NCC report artifact failed its integrity check.",
        )
    return NccWeeklyArtifact(
        filename=run.artifact_filename,
        content_type=run.artifact_content_type,
        content=run.artifact_content,
        sha256=run.artifact_sha256,
    )


def run_scheduled_ncc_report_email(db: Session) -> NccWeeklyDeliveryOutcome:
    """Compatibility entry point for callers migrating to the typed task command."""

    observed_at = datetime.now(UTC)
    return run_due_delivery(
        db=db,
        command=RunNccWeeklyDeliveryCommand(
            context=CommandContext.system(
                actor="celery:ncc-weekly-report",
                scope="ncc.weekly_report",
                reason="evaluate scheduled NCC weekly report delivery",
                idempotency_key=f"ncc-weekly-check:{observed_at:%Y%m%d%H%M}",
            ),
            observed_at=observed_at,
        ),
    )
