"""System export tool service helpers."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import smtplib
import zipfile
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import Invoice, Payment
from app.models.catalog import NasDevice, Subscription
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.provisioning import ServiceOrder
from app.models.scheduler import ScheduledTask, ScheduleType
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.services import email as email_service
from app.services import settings_spec
from app.services.audit_adapter import record_audit_event

logger = logging.getLogger(__name__)

EXPORT_CONFIG: dict[str, dict[str, Any]] = {
    "subscribers": {
        "label": "Subscribers",
        "model": Subscriber,
        "date_field": "created_at",
        "status_field": "status",
    },
    "subscriptions": {
        "label": "Subscriptions",
        "model": Subscription,
        "date_field": "created_at",
        "status_field": "status",
    },
    "invoices": {
        "label": "Invoices",
        "model": Invoice,
        "date_field": "created_at",
        "status_field": "status",
    },
    "payments": {
        "label": "Payments",
        "model": Payment,
        "date_field": "created_at",
        "status_field": "status",
    },
    "nas_devices": {
        "label": "NAS Devices",
        "model": NasDevice,
        "date_field": "created_at",
        "status_field": "status",
    },
    "service_orders": {
        "label": "Service Orders",
        "model": ServiceOrder,
        "date_field": "created_at",
        "status_field": "status",
    },
    "audit_log": {
        "label": "Audit Log",
        "model": AuditEvent,
        "date_field": "occurred_at",
        "status_field": None,
    },
    "users": {
        "label": "System Users",
        "model": SystemUser,
        "date_field": "created_at",
        "status_field": None,
    },
}

DELIMITER_OPTIONS = [
    {"id": ",", "label": "Comma (,)"},
    {"id": ";", "label": "Semicolon (;)"},
    {"id": "\\t", "label": "Tab"},
    {"id": "|", "label": "Pipe (|)"},
]
EXPORT_FORMAT_OPTIONS = [
    {"id": "csv", "label": "CSV"},
    {"id": "xlsx", "label": "Excel (.xlsx)"},
    {"id": "json", "label": "JSON"},
    {"id": "pdf", "label": "PDF"},
]
SCHEDULE_FREQUENCY_OPTIONS = [
    {"id": "hourly", "label": "Hourly"},
    {"id": "daily", "label": "Daily"},
    {"id": "weekly", "label": "Weekly"},
    {"id": "custom", "label": "Custom (hours)"},
]
EXPORT_SCHEDULE_TASK_NAME = "app.tasks.exports.run_scheduled_export"
EXPORT_TEMPLATE_KEY_PREFIX = "export_template."
EXPORT_JOB_KEY_PREFIX = "export_job."
EXPORT_BG_THRESHOLD_ROWS = 10000
EXPORT_JOBS_DIR = Path("uploads/system_exports")


def module_options() -> list[dict[str, str]]:
    return [
        {"id": key, "label": value["label"]} for key, value in EXPORT_CONFIG.items()
    ]


def _coerce_delimiter(value: str | None) -> str:
    token = (value or ",").strip().lower()
    mapping = {",": ",", ";": ";", "\\t": "\t", "tab": "\t", "|": "|"}
    return mapping.get(token, ",")


def module_fields(module: str) -> list[str]:
    cfg = EXPORT_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported export module")
    model = cfg["model"]
    return [column.key for column in model.__table__.columns]


def module_status_options(module: str) -> list[str]:
    cfg = EXPORT_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported export module")
    status_field = cfg.get("status_field")
    if not status_field:
        return []
    model = cfg["model"]
    column = model.__table__.columns.get(status_field)
    if column is None:
        return []
    enum_cls = getattr(column.type, "enum_class", None)
    if enum_cls is None:
        return []
    return [str(item.value) for item in enum_cls]


def _serialize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _date_filters(
    date_from: str | None, date_to: str | None
) -> tuple[datetime | None, datetime | None]:
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if date_from:
        start_date = date.fromisoformat(date_from)
        start_dt = datetime.combine(start_date, time.min, tzinfo=UTC)
    if date_to:
        end_date = date.fromisoformat(date_to)
        end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt, end_dt


def export_csv(
    db: Session,
    *,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    include_headers: bool = True,
    max_rows: int | None = 10000,
) -> tuple[str, int]:
    rows, fields = _query_rows(
        db,
        module=module,
        selected_fields=selected_fields,
        date_from=date_from,
        date_to=date_to,
        status=status,
        max_rows=max_rows,
    )
    output = io.StringIO()
    writer = csv.writer(output, delimiter=_coerce_delimiter(delimiter))

    if include_headers:
        writer.writerow(fields)
    for row in rows:
        writer.writerow(
            [_serialize_value(getattr(row, field, None)) for field in fields]
        )

    return output.getvalue(), len(rows)


def _query_rows(
    db: Session,
    *,
    module: str,
    selected_fields: list[str] | None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    max_rows: int | None = 10000,
) -> tuple[list[Any], list[str]]:
    cfg = EXPORT_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported export module")

    all_fields = module_fields(module)
    if selected_fields:
        fields = [field for field in selected_fields if field in all_fields]
    else:
        fields = all_fields
    if not fields:
        raise ValueError("Select at least one field")

    model = cfg["model"]
    query = db.query(model)
    start_dt, end_dt = _date_filters(date_from, date_to)

    date_field_name = cfg.get("date_field")
    if date_field_name and hasattr(model, date_field_name):
        date_column = getattr(model, date_field_name)
        if start_dt is not None:
            query = query.filter(date_column >= start_dt)
        if end_dt is not None:
            query = query.filter(date_column < end_dt)

    status_field_name = cfg.get("status_field")
    if status and status_field_name and hasattr(model, status_field_name):
        status_column = getattr(model, status_field_name)
        query = query.filter(status_column == status)

    if date_field_name and hasattr(model, date_field_name):
        query = query.order_by(getattr(model, date_field_name).desc())

    if max_rows is not None:
        rows = query.limit(max_rows).all()
    else:
        rows = query.all()
    return rows, fields


def count_rows(
    db: Session,
    *,
    module: str,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> int:
    cfg = EXPORT_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported export module")
    model = cfg["model"]
    query = db.query(func.count()).select_from(model)
    start_dt, end_dt = _date_filters(date_from, date_to)

    date_field_name = cfg.get("date_field")
    if date_field_name and hasattr(model, date_field_name):
        date_column = getattr(model, date_field_name)
        if start_dt is not None:
            query = query.filter(date_column >= start_dt)
        if end_dt is not None:
            query = query.filter(date_column < end_dt)

    status_field_name = cfg.get("status_field")
    if status and status_field_name and hasattr(model, status_field_name):
        status_column = getattr(model, status_field_name)
        query = query.filter(status_column == status)

    return int(query.scalar() or 0)


def _table_rows(rows: list[Any], fields: list[str]) -> list[list[str]]:
    return [
        [_serialize_value(getattr(row, field, None)) for field in fields]
        for row in rows
    ]


def _xlsx_col_name(index: int) -> str:
    out = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _render_xlsx(
    fields: list[str], data_rows: list[list[str]], include_headers: bool
) -> bytes:
    sheet_rows: list[list[str]] = []
    if include_headers:
        sheet_rows.append(fields)
    sheet_rows.extend(data_rows)

    row_xml: list[str] = []
    for row_idx, row in enumerate(sheet_rows, start=1):
        cells: list[str] = []
        for col_idx, value in enumerate(row):
            ref = f"{_xlsx_col_name(col_idx)}{row_idx}"
            safe = (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{safe}</t></is></c>')
        row_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Export" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _escape_pdf_text(value: str) -> str:
    ascii_text = value.encode("ascii", errors="replace").decode("ascii")
    return ascii_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _render_pdf(
    module_label: str,
    fields: list[str],
    data_rows: list[list[str]],
    include_headers: bool,
) -> bytes:
    lines: list[str] = [f"{module_label} Export", f"Rows: {len(data_rows)}", ""]
    if include_headers:
        lines.append(" | ".join(fields))
        lines.append("-" * 100)
    for row in data_rows[:60]:
        lines.append(" | ".join(row))
    if len(data_rows) > 60:
        lines.append(f"... truncated {len(data_rows) - 60} row(s)")

    stream_parts = ["BT", "/F1 10 Tf", "50 760 Td"]
    for idx, line in enumerate(lines):
        escaped = _escape_pdf_text(line)
        if idx == 0:
            stream_parts.append(f"({escaped}) Tj")
        else:
            stream_parts.append("0 -14 Td")
            stream_parts.append(f"({escaped}) Tj")
    stream_parts.append("ET")
    stream_data = "\n".join(stream_parts).encode("ascii")

    objects: list[bytes] = []
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objects.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
    )
    objects.append(
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    )
    objects.append(
        b"5 0 obj << /Length "
        + str(len(stream_data)).encode("ascii")
        + b" >> stream\n"
        + stream_data
        + b"\nendstream endobj\n"
    )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return out.getvalue()


def export_content(
    db: Session,
    *,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    include_headers: bool = True,
    max_rows: int | None = 10000,
) -> tuple[bytes, str, str, int]:
    rows, fields = _query_rows(
        db,
        module=module,
        selected_fields=selected_fields,
        date_from=date_from,
        date_to=date_to,
        status=status,
        max_rows=max_rows,
    )
    data_rows = _table_rows(rows, fields)
    normalized = (export_format or "csv").strip().lower()
    if normalized == "csv":
        text, count = export_csv(
            db,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
            max_rows=max_rows,
        )
        return text.encode("utf-8"), "text/csv", "csv", count
    if normalized == "json":
        payload = [
            {field: row[idx] for idx, field in enumerate(fields)} for row in data_rows
        ]
        body = json.dumps(payload, indent=2)
        return body.encode("utf-8"), "application/json", "json", len(data_rows)
    if normalized == "xlsx":
        body = _render_xlsx(fields, data_rows, include_headers=include_headers)
        return (
            body,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
            len(data_rows),
        )
    if normalized == "pdf":
        module_label = EXPORT_CONFIG[module]["label"]
        body = _render_pdf(
            module_label, fields, data_rows, include_headers=include_headers
        )
        return body, "application/pdf", "pdf", len(data_rows)
    raise ValueError("Unsupported export format")


def _interval_seconds_for_frequency(
    *,
    frequency: str,
    custom_interval_hours: int | None,
) -> int:
    normalized = (frequency or "weekly").strip().lower()
    if normalized == "hourly":
        return 3600
    if normalized == "daily":
        return 86400
    if normalized == "weekly":
        return 86400 * 7
    if normalized == "custom":
        hours = max(custom_interval_hours or 1, 1)
        return hours * 3600
    raise ValueError("Unsupported schedule frequency")


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    return normalized or "template"


def _parse_template_setting(setting: DomainSetting) -> dict[str, Any]:
    payload = setting.value_json if isinstance(setting.value_json, dict) else {}
    template_id = setting.key.replace(EXPORT_TEMPLATE_KEY_PREFIX, "", 1)
    return {
        "id": template_id,
        "name": str(payload.get("name") or template_id),
        "config": payload.get("config")
        if isinstance(payload.get("config"), dict)
        else {},
        "created_at": setting.created_at,
        "updated_at": setting.updated_at,
    }


def _parse_export_job_setting(setting: DomainSetting) -> dict[str, Any]:
    payload = setting.value_json if isinstance(setting.value_json, dict) else {}
    job_id = setting.key.replace(EXPORT_JOB_KEY_PREFIX, "", 1)
    return {
        "id": job_id,
        "status": str(payload.get("status") or "queued"),
        "row_count": int(payload.get("row_count") or 0),
        "module": str(payload.get("module") or ""),
        "export_format": str(payload.get("export_format") or "csv"),
        "filename": str(payload.get("filename") or ""),
        "file_path": str(payload.get("file_path") or ""),
        "download_url": str(payload.get("download_url") or ""),
        "error": payload.get("error"),
        "requested_by_email": str(payload.get("requested_by_email") or ""),
        "recipient_email": str(payload.get("recipient_email") or ""),
        "queued_at": str(payload.get("queued_at") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "completed_at": str(payload.get("completed_at") or ""),
        "config": payload.get("config")
        if isinstance(payload.get("config"), dict)
        else {},
    }


def list_export_jobs(db: Session, *, limit: int = 25) -> list[dict[str, Any]]:
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key.like(f"{EXPORT_JOB_KEY_PREFIX}%"))
        .filter(DomainSetting.is_active.is_(True))
        .order_by(DomainSetting.created_at.desc())
        .limit(max(limit, 1))
        .all()
    )
    return [_parse_export_job_setting(item) for item in settings]


def get_export_job(db: Session, job_id: str) -> dict[str, Any] | None:
    key = f"{EXPORT_JOB_KEY_PREFIX}{job_id.strip()}"
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    return _parse_export_job_setting(setting)


def list_export_templates(db: Session) -> list[dict[str, Any]]:
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key.like(f"{EXPORT_TEMPLATE_KEY_PREFIX}%"))
        .filter(DomainSetting.is_active.is_(True))
        .order_by(DomainSetting.created_at.desc())
        .all()
    )
    return [_parse_template_setting(item) for item in settings]


def get_export_template(db: Session, template_id: str) -> dict[str, Any] | None:
    key = f"{EXPORT_TEMPLATE_KEY_PREFIX}{template_id.strip()}"
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    return _parse_template_setting(setting)


def _normalize_export_config(
    *,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    include_headers: bool,
) -> dict[str, Any]:
    valid_fields = module_fields(module)
    filtered_fields = [
        field for field in (selected_fields or []) if field in valid_fields
    ]
    if not filtered_fields:
        raise ValueError("Select at least one valid field")
    normalized_format = (export_format or "csv").strip().lower()
    if normalized_format not in {"csv", "xlsx", "json", "pdf"}:
        raise ValueError("Unsupported export format")
    normalized_status = (status or "").strip() or None
    if normalized_status:
        allowed_statuses = set(module_status_options(module))
        if allowed_statuses and normalized_status not in allowed_statuses:
            raise ValueError("Unsupported status filter")
    return {
        "module": module,
        "selected_fields": filtered_fields,
        "delimiter": delimiter or ",",
        "export_format": normalized_format,
        "date_from": (date_from or "").strip() or None,
        "date_to": (date_to or "").strip() or None,
        "status": normalized_status,
        "include_headers": bool(include_headers),
    }


def create_export_template(
    db: Session,
    *,
    name: str,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    include_headers: bool,
) -> dict[str, Any]:
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ValueError("Template name is required")
    config = _normalize_export_config(
        module=module,
        selected_fields=selected_fields,
        delimiter=delimiter,
        export_format=export_format,
        date_from=date_from,
        date_to=date_to,
        status=status,
        include_headers=include_headers,
    )
    template_id = (
        f"{_slugify(normalized_name)[:60]}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )
    key = f"{EXPORT_TEMPLATE_KEY_PREFIX}{template_id}"
    setting = DomainSetting(
        domain=SettingDomain.imports,
        key=key,
        value_type=SettingValueType.json,
        value_text=None,
        value_json={"name": normalized_name, "config": config},
        is_secret=False,
        is_active=True,
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)
    return _parse_template_setting(setting)


def delete_export_template(db: Session, *, template_id: str) -> None:
    key = f"{EXPORT_TEMPLATE_KEY_PREFIX}{template_id.strip()}"
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        raise ValueError("Export template not found")
    setting.is_active = False
    db.commit()


def create_export_job(
    db: Session,
    *,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    include_headers: bool,
    recipient_email: str | None,
    requested_by_email: str | None,
    row_count: int,
) -> dict[str, Any]:
    config = _normalize_export_config(
        module=module,
        selected_fields=selected_fields,
        delimiter=delimiter,
        export_format=export_format,
        date_from=date_from,
        date_to=date_to,
        status=status,
        include_headers=include_headers,
    )
    now = datetime.now(UTC).isoformat()
    job_id = str(uuid4())
    key = f"{EXPORT_JOB_KEY_PREFIX}{job_id}"
    setting = DomainSetting(
        domain=SettingDomain.imports,
        key=key,
        value_type=SettingValueType.json,
        value_text=None,
        value_json={
            "status": "queued",
            "row_count": int(row_count),
            "module": config["module"],
            "export_format": config["export_format"],
            "filename": "",
            "file_path": "",
            "download_url": f"/admin/system/export/jobs/{job_id}/download",
            "error": None,
            "requested_by_email": (requested_by_email or "").strip(),
            "recipient_email": (recipient_email or "").strip(),
            "queued_at": now,
            "started_at": "",
            "completed_at": "",
            "config": config,
        },
        is_secret=False,
        is_active=True,
    )
    db.add(setting)
    db.commit()
    return _parse_export_job_setting(setting)


def _set_export_job_state(
    db: Session, *, job_id: str, updates: dict[str, Any]
) -> dict[str, Any]:
    key = f"{EXPORT_JOB_KEY_PREFIX}{job_id.strip()}"
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        raise ValueError("Export job not found")
    payload = dict(setting.value_json) if isinstance(setting.value_json, dict) else {}
    payload.update(updates)
    setting.value_json = payload
    db.commit()
    db.refresh(setting)
    return _parse_export_job_setting(setting)


def _write_export_job_file(job_id: str, extension: str, content: bytes) -> str:
    safe_ext = (extension or "csv").strip().lower()
    EXPORT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{job_id}.{safe_ext}"
    path = EXPORT_JOBS_DIR / filename
    path.write_bytes(content)
    return str(path)


def _resolve_export_link_base(db: Session) -> str:
    env_url = (os.getenv("APP_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    setting_url = settings_spec.resolve_value(db, SettingDomain.notification, "app_url")
    return str(setting_url or "").strip().rstrip("/")


def _send_export_link_email(
    db: Session,
    *,
    recipient_email: str,
    module: str,
    row_count: int,
    download_url: str,
) -> bool:
    from app.services.email_template import wrap_email_html

    subject = f"Export Ready: {EXPORT_CONFIG.get(module, {}).get('label', module)}"
    return email_service.send_email(
        db=db,
        to_email=recipient_email,
        subject=subject,
        body_html=wrap_email_html(
            (
                "<p>Your export file is ready.</p>"
                f"<p>Module: <strong>{module}</strong><br>"
                f"Rows: <strong>{row_count}</strong></p>"
                f'<p><a href="{download_url}">Download export file</a></p>'
            ),
            subject=subject,
        ),
        body_text=(
            f"Your export file is ready.\n\nModule: {module}\nRows: {row_count}\n\n"
            f"Download: {download_url}"
        ),
        track=True,
        activity="notification_queue",
    )


def log_export_audit_event(
    db: Session,
    *,
    action: str,
    module: str | None,
    actor_id: str | None,
    actor_type: AuditActorType = AuditActorType.system,
    entity_type: str = "system_export",
    entity_id: str | None = None,
    is_success: bool = True,
    status_code: int = 200,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_audit_event(
        db,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        status_code=status_code,
        is_success=is_success,
        metadata={
            "module": module,
            **(metadata or {}),
        },
    )


def process_export_job(db: Session, *, job_id: str) -> dict[str, Any]:
    job = get_export_job(db, job_id)
    if not job:
        raise ValueError("Export job not found")
    config = job.get("config") if isinstance(job.get("config"), dict) else {}
    _set_export_job_state(
        db,
        job_id=job_id,
        updates={
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "error": None,
        },
    )
    try:
        content, media_type, extension, row_count = export_content(
            db,
            module=str(config.get("module") or ""),
            selected_fields=[str(x) for x in (config.get("selected_fields") or [])],
            delimiter=str(config.get("delimiter") or ","),
            export_format=str(config.get("export_format") or "csv"),
            date_from=(str(config.get("date_from") or "").strip() or None),
            date_to=(str(config.get("date_to") or "").strip() or None),
            status=(str(config.get("status") or "").strip() or None),
            include_headers=bool(config.get("include_headers", True)),
            max_rows=None,
        )
        file_path = _write_export_job_file(job_id, extension, content)
        filename = _build_export_filename(
            str(config.get("module") or "export"), extension, row_count
        )
        base = _resolve_export_link_base(db)
        relative = f"/admin/system/export/jobs/{job_id}/download"
        download_url = f"{base}{relative}" if base else relative
        result = _set_export_job_state(
            db,
            job_id=job_id,
            updates={
                "status": "completed",
                "row_count": int(row_count),
                "filename": filename,
                "file_path": file_path,
                "download_url": download_url,
                "media_type": media_type,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        log_export_audit_event(
            db,
            action="export_job_completed",
            module=str(config.get("module") or ""),
            actor_id=None,
            actor_type=AuditActorType.system,
            entity_type="export_job",
            entity_id=job_id,
            is_success=True,
            status_code=200,
            metadata={
                "row_count": int(row_count),
                "format": str(config.get("export_format") or "csv"),
            },
        )
        recipient = str(job.get("recipient_email") or "").strip()
        if recipient:
            _send_export_link_email(
                db,
                recipient_email=recipient,
                module=str(config.get("module") or ""),
                row_count=int(row_count),
                download_url=download_url,
            )
        return result
    except Exception as exc:
        result = _set_export_job_state(
            db,
            job_id=job_id,
            updates={
                "status": "failed",
                "error": str(exc),
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        log_export_audit_event(
            db,
            action="export_job_failed",
            module=str(config.get("module") or ""),
            actor_id=None,
            actor_type=AuditActorType.system,
            entity_type="export_job",
            entity_id=job_id,
            is_success=False,
            status_code=500,
            metadata={"error": str(exc)},
        )
        return result


def list_export_schedules(db: Session) -> list[ScheduledTask]:
    return (
        db.query(ScheduledTask)
        .filter(ScheduledTask.task_name == EXPORT_SCHEDULE_TASK_NAME)
        .order_by(ScheduledTask.created_at.desc())
        .all()
    )


def create_export_schedule(
    db: Session,
    *,
    name: str,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    include_headers: bool,
    recipient_email: str,
    frequency: str,
    custom_interval_hours: int | None = None,
) -> ScheduledTask:
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ValueError("Schedule name is required")
    recipient = (recipient_email or "").strip()
    if not recipient:
        raise ValueError("Recipient email is required")
    if not selected_fields:
        raise ValueError("Select at least one field")
    config = _normalize_export_config(
        module=module,
        selected_fields=selected_fields,
        delimiter=delimiter,
        export_format=export_format,
        date_from=date_from,
        date_to=date_to,
        status=status,
        include_headers=include_headers,
    )

    interval_seconds = _interval_seconds_for_frequency(
        frequency=frequency,
        custom_interval_hours=custom_interval_hours,
    )
    task = ScheduledTask(
        name=normalized_name,
        task_name=EXPORT_SCHEDULE_TASK_NAME,
        schedule_type=ScheduleType.interval,
        interval_seconds=interval_seconds,
        enabled=True,
    )
    db.add(task)
    db.flush()
    task.kwargs_json = {
        "schedule_task_id": str(task.id),
        "module": config["module"],
        "selected_fields": config["selected_fields"],
        "delimiter": config["delimiter"],
        "export_format": config["export_format"],
        "date_from": config["date_from"],
        "date_to": config["date_to"],
        "status": config["status"],
        "include_headers": config["include_headers"],
        "recipient_email": recipient,
        "frequency": frequency,
        "custom_interval_hours": custom_interval_hours,
    }
    db.commit()
    db.refresh(task)
    return task


def set_export_schedule_enabled(
    db: Session, *, schedule_id: str, enabled: bool
) -> ScheduledTask:
    task = db.get(ScheduledTask, schedule_id)
    if task is None or task.task_name != EXPORT_SCHEDULE_TASK_NAME:
        raise ValueError("Scheduled export not found")
    task.enabled = enabled
    db.commit()
    db.refresh(task)
    return task


def delete_export_schedule(db: Session, *, schedule_id: str) -> None:
    task = db.get(ScheduledTask, schedule_id)
    if task is None or task.task_name != EXPORT_SCHEDULE_TASK_NAME:
        raise ValueError("Scheduled export not found")
    db.delete(task)
    db.commit()


def _build_export_filename(module: str, extension: str, row_count: int) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"scheduled_export_{module}_{row_count}_{timestamp}.{extension}"


def _send_export_email(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body_html: str,
    filename: str,
    content: bytes,
    media_type: str,
) -> bool:
    from app.services.branding_config import get_brand

    config = email_service.get_smtp_config(db, activity="notification_queue")
    brand = get_brand()
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = (
        f"{config.get('from_name') or brand['from_name']} "
        f"<{config.get('from_email') or brand['from_email']}>"
    )
    msg["To"] = to_email
    from app.services.email_template import render_email_bodies

    # Wrap fragments/plain text in the branded template; leave full documents.
    if "<html" not in body_html.lower():
        body_html, _ = render_email_bodies(body_html, subject=subject)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_html, "html"))
    msg.attach(alt)

    part = MIMEApplication(content)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    try:
        host = str(config.get("host") or "")
        if not host:
            return False
        port = int(config.get("port", 587) or 587)
        if bool(config.get("use_ssl")):
            server = smtplib.SMTP_SSL(host, port)
        else:
            server = smtplib.SMTP(host, port)
        if config.get("use_tls") and not config.get("use_ssl"):
            server.starttls()
        username = config.get("username")
        password = config.get("password")
        if username and password:
            server.login(username, password)
        server.sendmail(config.get("from_email"), [to_email], msg.as_string())
        server.quit()
        return True
    except Exception:
        return False


def execute_scheduled_export(
    db: Session,
    *,
    schedule_task_id: str | None,
    module: str,
    selected_fields: list[str] | None,
    delimiter: str,
    export_format: str,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    include_headers: bool,
    recipient_email: str | None,
) -> dict[str, Any]:
    recipient = (recipient_email or "").strip()
    if not recipient:
        fallback = settings_spec.resolve_value(
            db,
            SettingDomain.notification,
            "alert_notifications_default_recipient",
        )
        recipient = str(fallback or "").strip()
    if not recipient:
        raise ValueError("Scheduled export recipient email is not configured")

    content, media_type, extension, row_count = export_content(
        db,
        module=module,
        selected_fields=selected_fields,
        delimiter=delimiter,
        export_format=export_format,
        date_from=date_from,
        date_to=date_to,
        status=status,
        include_headers=include_headers,
    )
    filename = _build_export_filename(module, extension, row_count)
    sent = _send_export_email(
        db,
        to_email=recipient,
        subject=f"Scheduled Export: {EXPORT_CONFIG.get(module, {}).get('label', module)}",
        body_html=(
            f"<p>Scheduled export completed.</p>"
            f"<p>Module: <strong>{module}</strong><br>"
            f"Rows: <strong>{row_count}</strong></p>"
        ),
        filename=filename,
        content=content,
        media_type=media_type,
    )
    if schedule_task_id:
        task = db.get(ScheduledTask, schedule_task_id)
        if task and task.task_name == EXPORT_SCHEDULE_TASK_NAME:
            task.last_run_at = datetime.now(UTC)
            db.commit()
    log_export_audit_event(
        db,
        action="export_scheduled_run",
        module=module,
        actor_id=None,
        actor_type=AuditActorType.system,
        entity_type="scheduled_export",
        entity_id=schedule_task_id,
        is_success=sent,
        status_code=200 if sent else 500,
        metadata={
            "row_count": row_count,
            "format": export_format,
            "recipient_email": recipient,
        },
    )
    return {
        "schedule_task_id": schedule_task_id,
        "module": module,
        "rows": row_count,
        "recipient_email": recipient,
        "sent": sent,
        "filename": filename,
    }
