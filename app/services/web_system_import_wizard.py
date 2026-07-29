"""System import wizard service helpers."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from xml.etree import ElementTree as ET  # nosec

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.catalog import (
    BillingMode,
    NasDevice,
    NasVendor,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.network import IPAssignment, IpPool, IPVersion
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
)
from app.models.network_monitoring import (
    DeviceStatus as MonitoringDeviceStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import job_log_store, settings_spec

logger = logging.getLogger(__name__)

IMPORT_HISTORY_KEY = "import_history_log"
IMPORT_ROLLBACK_WINDOW_KEY = "import_rollback_window_hours"
IMPORT_BACKGROUND_THRESHOLD_KEY = "import_background_threshold_rows"
IMPORT_JOBS_KEY = "import_jobs_log"
DEFAULT_ROLLBACK_WINDOW_HOURS = 24
DEFAULT_BACKGROUND_THRESHOLD_ROWS = 1000


class SubscriberImportRow(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    status: SubscriberStatus = SubscriberStatus.active
    is_active: bool = True


class SubscriptionImportRow(BaseModel):
    subscriber_id: uuid.UUID
    offer_id: uuid.UUID
    status: SubscriptionStatus = SubscriptionStatus.pending
    billing_mode: BillingMode | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    next_billing_at: datetime | None = None
    canceled_at: datetime | None = None
    cancel_reason: str | None = None


class InvoiceImportRow(BaseModel):
    account_id: uuid.UUID
    invoice_number: str | None = None
    status: InvoiceStatus = InvoiceStatus.draft
    billing_mode: BillingMode | None = None
    currency: str = "NGN"
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    balance_due: Decimal = Decimal("0.00")
    memo: str | None = None


class PaymentImportRow(BaseModel):
    account_id: uuid.UUID
    amount: Decimal
    currency: str = "NGN"
    status: PaymentStatus = PaymentStatus.succeeded
    memo: str | None = None
    external_id: str = Field(min_length=1, max_length=255)
    invoice_number: str | None = None


class NasDeviceImportRow(BaseModel):
    name: str
    management_ip: str | None = None
    vendor: NasVendor = NasVendor.other
    is_active: bool = True


class IpPoolImportRow(BaseModel):
    name: str
    ip_version: IPVersion = IPVersion.ipv4
    cidr: str
    gateway: str | None = None
    dns_primary: str | None = None
    dns_secondary: str | None = None
    is_active: bool = True


class NetworkEquipmentImportRow(BaseModel):
    name: str
    pop_site_id: uuid.UUID | None = None
    hostname: str | None = None
    mgmt_ip: str | None = None
    vendor: str | None = None
    model: str | None = None
    role: DeviceRole = DeviceRole.edge
    status: MonitoringDeviceStatus = MonitoringDeviceStatus.offline
    is_active: bool = True


class Ipv4AssignmentImportRow(BaseModel):
    pool_id: uuid.UUID
    ip_address: str
    subscriber_id: uuid.UUID
    subscription_id: uuid.UUID | None = None


ENTITY_CONFIG: dict[str, dict[str, Any]] = {
    "subscribers": {
        "label": "Subscribers",
        "model": SubscriberImportRow,
        "headers": ["first_name", "last_name", "email", "phone", "status", "is_active"],
    },
    "subscriptions": {
        "label": "Subscriptions",
        "model": SubscriptionImportRow,
        "headers": [
            "subscriber_id",
            "offer_id",
            "status",
            "billing_mode",
            "start_at",
            "end_at",
            "next_billing_at",
            "canceled_at",
            "cancel_reason",
        ],
    },
    "invoices": {
        "label": "Invoices",
        "model": InvoiceImportRow,
        "headers": [
            "account_id",
            "invoice_number",
            "status",
            "billing_mode",
            "currency",
            "subtotal",
            "tax_total",
            "total",
            "balance_due",
            "memo",
        ],
    },
    "payments": {
        "label": "Payments",
        "model": PaymentImportRow,
        "headers": [
            "account_id",
            "amount",
            "currency",
            "status",
            "memo",
            "external_id",
            "invoice_number",
        ],
    },
    "nas_devices": {
        "label": "NAS Devices",
        "model": NasDeviceImportRow,
        "headers": ["name", "management_ip", "vendor", "is_active"],
    },
    "ip_pools": {
        "label": "IP Address Pools",
        "model": IpPoolImportRow,
        "headers": [
            "name",
            "ip_version",
            "cidr",
            "gateway",
            "dns_primary",
            "dns_secondary",
            "is_active",
        ],
    },
    "network_equipment": {
        "label": "Network Equipment",
        "model": NetworkEquipmentImportRow,
        "headers": [
            "name",
            "pop_site_id",
            "hostname",
            "mgmt_ip",
            "vendor",
            "model",
            "role",
            "status",
            "is_active",
        ],
    },
    "ipv4_assignments": {
        "label": "IPv4 Address Assignments",
        "model": Ipv4AssignmentImportRow,
        "headers": ["pool_id", "ip_address", "subscriber_id", "subscription_id"],
    },
}

ENTITY_MODEL_MAP: dict[str, type] = {
    "subscribers": Subscriber,
    "subscriptions": Subscription,
    "invoices": Invoice,
    "payments": Payment,
    "nas_devices": NasDevice,
    "ip_pools": IpPool,
    "network_equipment": NetworkDevice,
    "ipv4_assignments": IPAssignment,
}


@dataclass
class ParsedPayload:
    rows: list[dict[str, Any]]
    source_name: str


def module_options() -> list[dict[str, str]]:
    return [
        {"id": key, "label": value["label"]} for key, value in ENTITY_CONFIG.items()
    ]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        k = str(key).strip()
        if not k:
            continue
        normalized[k] = value.strip() if isinstance(value, str) else value
    return normalized


def _resolve_csv_delimiter(value: str | None) -> str:
    token = (value or ",").strip().lower()
    mapping = {
        ",": ",",
        "comma": ",",
        ";": ";",
        "semicolon": ";",
        "\\t": "\t",
        "tab": "\t",
        "|": "|",
        "pipe": "|",
    }
    return mapping.get(token, ",")


def _column_ref_to_index(cell_ref: str) -> int | None:
    match = re.match(r"^([A-Za-z]+)", cell_ref or "")
    if not match:
        return None
    letters = match.group(1).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _parse_xlsx_rows(file_bytes: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))  # nosec  # noqa: S314
            for si in strings_root.findall(".//{*}si"):
                text = "".join(node.text or "" for node in si.findall(".//{*}t"))
                shared_strings.append(text)

    root = ET.fromstring(sheet_xml)  # nosec  # noqa: S314
    row_maps: list[dict[int, Any]] = []
    for row_node in root.findall(".//{*}sheetData/{*}row"):
        row_map: dict[int, Any] = {}
        for cell in row_node.findall("{*}c"):
            ref = cell.attrib.get("r", "")
            col_idx = _column_ref_to_index(ref)
            if col_idx is None:
                continue
            cell_type = cell.attrib.get("t")
            value = ""
            if cell_type == "s":
                raw_idx = cell.findtext("{*}v", default="").strip()
                if raw_idx.isdigit():
                    idx = int(raw_idx)
                    value = (
                        shared_strings[idx] if 0 <= idx < len(shared_strings) else ""
                    )
            elif cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//{*}t"))
            else:
                value = cell.findtext("{*}v", default="")
            row_map[col_idx] = value
        row_maps.append(row_map)

    if not row_maps:
        return []

    header_map = row_maps[0]
    header_indexes = sorted(header_map)
    headers = [str(header_map[idx]).strip() for idx in header_indexes]
    rows: list[dict[str, Any]] = []
    for row_map in row_maps[1:]:
        row: dict[str, Any] = {}
        for pos, col_idx in enumerate(header_indexes):
            key = headers[pos]
            if not key:
                continue
            row[key] = row_map.get(col_idx, "")
        rows.append(_normalize_row(row))
    return rows


def parse_payload(
    *,
    data_format: str,
    raw_text: str,
    source_name: str = "manual",
    csv_delimiter: str = ",",
    file_bytes: bytes | None = None,
) -> ParsedPayload:
    normalized_format = (data_format or "csv").strip().lower()
    if normalized_format not in {"csv", "json", "xlsx"}:
        raise ValueError("Unsupported format. Use csv, json, or xlsx")
    if not raw_text.strip() and not file_bytes:
        raise ValueError("Import data is required")

    if normalized_format == "csv":
        delimiter = _resolve_csv_delimiter(csv_delimiter)
        content = raw_text
        if file_bytes is not None:
            content = file_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        rows = [_normalize_row(row) for row in reader]
        return ParsedPayload(rows=rows, source_name=source_name or "upload.csv")
    if normalized_format == "xlsx":
        if file_bytes is None:
            raise ValueError("XLSX imports require an uploaded .xlsx file")
        rows = _parse_xlsx_rows(file_bytes)
        return ParsedPayload(rows=rows, source_name=source_name or "upload.xlsx")

    payload = json.loads(raw_text)
    if isinstance(payload, dict):
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("JSON object payload must include an 'items' array")
        rows = [_normalize_row(item) for item in items if isinstance(item, dict)]
    elif isinstance(payload, list):
        rows = [_normalize_row(item) for item in payload if isinstance(item, dict)]
    else:
        raise ValueError("JSON payload must be an array or object with 'items'")
    return ParsedPayload(rows=rows, source_name=source_name or "upload.json")


def detect_columns_and_preview(
    *,
    data_format: str,
    raw_text: str,
    csv_delimiter: str = ",",
    file_bytes: bytes | None = None,
    max_rows: int = 5,
) -> dict[str, Any]:
    parsed = parse_payload(
        data_format=data_format,
        raw_text=raw_text,
        source_name="preview",
        csv_delimiter=csv_delimiter,
        file_bytes=file_bytes,
    )
    columns: list[str] = []
    for row in parsed.rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return {
        "columns": columns,
        "preview_rows": parsed.rows[:max_rows],
        "row_count": len(parsed.rows),
    }


def apply_column_mapping(
    rows: list[dict[str, Any]], mapping: dict[str, str]
) -> list[dict[str, Any]]:
    if not mapping:
        return rows
    mapped_rows: list[dict[str, Any]] = []
    for row in rows:
        mapped: dict[str, Any] = {}
        for key, value in row.items():
            target = (mapping.get(key) or "").strip()
            mapped[target or key] = value
        mapped_rows.append(mapped)
    return mapped_rows


def _validate_rows(
    module: str, rows: list[dict[str, Any]]
) -> tuple[list[Any], list[dict[str, Any]]]:
    cfg = ENTITY_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported import module")
    model_cls: type[BaseModel] = cfg["model"]
    valid_rows: list[Any] = []
    errors: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        try:
            valid_rows.append(model_cls.model_validate(row))
        except ValidationError as exc:
            errors.append({"row": idx, "detail": str(exc)})
    return valid_rows, errors


def _persist_row(
    db: Session,
    module: str,
    parsed_row: Any,
    *,
    source_name: str | None = None,
    with_provenance: bool = False,
) -> Any:
    from app.services.financial_imports import (
        FINANCIAL_IMPORT_MODULES,
        persist_import_row,
    )

    if module in FINANCIAL_IMPORT_MODULES:
        return persist_import_row(
            db,
            module,
            parsed_row,
            source_name=source_name or "import",
            with_provenance=with_provenance,
        )
    if module == "subscribers":
        obj = Subscriber(
            first_name=parsed_row.first_name,
            last_name=parsed_row.last_name,
            email=parsed_row.email,
            phone=parsed_row.phone,
            status=parsed_row.status,
            is_active=parsed_row.is_active,
        )
        db.add(obj)
        return obj
    if module == "nas_devices":
        obj = NasDevice(
            name=parsed_row.name,
            management_ip=parsed_row.management_ip,
            vendor=parsed_row.vendor,
            is_active=parsed_row.is_active,
        )
        db.add(obj)
        return obj
    if module == "ip_pools":
        obj = IpPool(
            name=parsed_row.name,
            ip_version=parsed_row.ip_version,
            cidr=parsed_row.cidr,
            gateway=parsed_row.gateway,
            dns_primary=parsed_row.dns_primary,
            dns_secondary=parsed_row.dns_secondary,
            is_active=parsed_row.is_active,
        )
        db.add(obj)
        return obj
    if module == "network_equipment":
        obj = NetworkDevice(
            name=parsed_row.name,
            pop_site_id=parsed_row.pop_site_id,
            hostname=parsed_row.hostname,
            mgmt_ip=parsed_row.mgmt_ip,
            vendor=parsed_row.vendor,
            model=parsed_row.model,
            role=parsed_row.role,
            status=parsed_row.status,
            is_active=parsed_row.is_active,
        )
        db.add(obj)
        return obj
    if module == "ipv4_assignments":
        from app.services.web_network_ip import assign_ipv4_for_import

        return assign_ipv4_for_import(
            db,
            pool_id=str(parsed_row.pool_id),
            ip_address=parsed_row.ip_address,
            subscriber_id=str(parsed_row.subscriber_id),
            subscription_id=(
                str(parsed_row.subscription_id) if parsed_row.subscription_id else None
            ),
        )
    raise ValueError("Unsupported import module")


def _history_entries(db: Session) -> list[dict[str, Any]]:
    try:
        setting = domain_settings_service.imports_settings.get_by_key(
            db, IMPORT_HISTORY_KEY
        )
    except Exception:
        return []
    if isinstance(setting.value_json, list):
        # Return detached entries so callers can build the next JSON value
        # without mutating the ORM's current value in place. SQLAlchemy cannot
        # reliably detect an in-place mutation of a plain JSON list.
        return [dict(item) for item in setting.value_json if isinstance(item, dict)]
    if isinstance(setting.value_text, str) and setting.value_text.strip():
        try:
            parsed = json.loads(setting.value_text)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    return []


def append_history(db: Session, entry: dict[str, Any]) -> None:
    history = _history_entries(db)
    history.insert(0, entry)
    history = history[:200]
    domain_settings_service.imports_settings.upsert_by_key(
        db,
        IMPORT_HISTORY_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=history,
            value_text=None,
            is_secret=False,
        ),
    )


def _job_entries(db: Session) -> list[dict[str, Any]]:
    return job_log_store.read_json_list(
        db, domain_settings_service.imports_settings, IMPORT_JOBS_KEY
    )


def _save_jobs(db: Session, jobs: list[dict[str, Any]]) -> None:
    job_log_store.save_json_list(
        db,
        domain_settings_service.imports_settings,
        IMPORT_JOBS_KEY,
        jobs,
        limit=200,
        is_secret=False,
        is_active=None,
    )


def list_jobs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    return _job_entries(db)[:limit]


def get_job(db: Session, job_id: str) -> dict[str, Any] | None:
    return job_log_store.get_job(_job_entries(db), job_id)


def upsert_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    jobs, merged = job_log_store.upsert_job(_job_entries(db), payload)
    _save_jobs(db, jobs)
    return merged


def background_threshold_rows(db: Session) -> int:
    raw = settings_spec.resolve_value(
        db, SettingDomain.imports, IMPORT_BACKGROUND_THRESHOLD_KEY
    )
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            if parsed >= 1:
                return parsed
    return DEFAULT_BACKGROUND_THRESHOLD_ROWS


def rollback_window_hours(db: Session) -> int:
    raw = settings_spec.resolve_value(
        db, SettingDomain.imports, IMPORT_ROLLBACK_WINDOW_KEY
    )
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            if parsed >= 1:
                return parsed
    return DEFAULT_ROLLBACK_WINDOW_HOURS


def list_history(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    db.expire_all()
    return _history_entries(db)[:limit]


def execute_import(
    db: Session,
    *,
    module: str,
    data_format: str,
    raw_text: str,
    source_name: str,
    dry_run: bool,
    column_mapping: dict[str, str] | None = None,
    csv_delimiter: str = ",",
    file_bytes: bytes | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from app.services.financial_imports import FINANCIAL_IMPORT_MODULES

    if module in FINANCIAL_IMPORT_MODULES and not dry_run:
        raise ValueError(
            "Financial and subscription imports require a durable dry run. "
            "Apply the validated run from Import Runs."
        )
    parsed = parse_payload(
        data_format=data_format,
        raw_text=raw_text,
        source_name=source_name,
        csv_delimiter=csv_delimiter,
        file_bytes=file_bytes,
    )
    mapped_rows = apply_column_mapping(parsed.rows, column_mapping or {})
    valid_rows, validation_errors = _validate_rows(module, mapped_rows)
    total_rows = len(mapped_rows)
    total_valid_rows = len(valid_rows)
    if progress_callback:
        progress_callback(
            {
                "phase": "validated",
                "total_rows": total_rows,
                "total_valid_rows": total_valid_rows,
                "processed_valid_rows": 0,
                "validated_rows": total_valid_rows,
                "imported_rows": 0,
                "failed_rows": len(validation_errors),
            }
        )

    imported = 0
    created_records: list[dict[str, str]] = []
    commit_errors: list[dict[str, Any]] = []

    if not dry_run:
        for idx, row in enumerate(valid_rows, start=1):
            nested = db.begin_nested()
            try:
                persisted = _persist_row(
                    db, module, row, source_name=parsed.source_name
                )
                db.flush()
                persisted_id = getattr(persisted, "id", None)
                if persisted_id is not None:
                    created_records.append({"module": module, "id": str(persisted_id)})
                nested.commit()
                imported += 1
            except Exception as exc:
                nested.rollback()
                commit_errors.append({"row": idx, "detail": str(exc)})
            if progress_callback:
                progress_callback(
                    {
                        "phase": "importing",
                        "total_rows": total_rows,
                        "total_valid_rows": total_valid_rows,
                        "processed_valid_rows": idx,
                        "validated_rows": total_valid_rows,
                        "imported_rows": imported,
                        "failed_rows": len(validation_errors) + len(commit_errors),
                    }
                )
        db.commit()

    all_errors = validation_errors + commit_errors
    status = "success"
    if all_errors and imported > 0:
        status = "partial"
    elif all_errors and imported == 0:
        status = "failed"
    if dry_run:
        status = "dry_run"

    summary = {
        "import_id": str(uuid.uuid4()),
        "module": module,
        "module_label": ENTITY_CONFIG[module]["label"],
        "format": data_format,
        "file_name": parsed.source_name,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_rows": len(mapped_rows),
        "validated_rows": len(valid_rows),
        "imported_rows": imported,
        "failed_rows": len(all_errors),
        "status": status,
        "errors": all_errors[:50],
        "dry_run": dry_run,
        "column_mapping": column_mapping or {},
        "csv_delimiter": _resolve_csv_delimiter(csv_delimiter),
        "created_records": [] if dry_run else created_records,
    }
    if progress_callback:
        progress_callback(
            {
                "phase": "completed",
                "total_rows": total_rows,
                "total_valid_rows": total_valid_rows,
                "processed_valid_rows": total_valid_rows,
                "validated_rows": total_valid_rows,
                "imported_rows": imported,
                "failed_rows": len(all_errors),
                "status": status,
            }
        )

    append_history(db, summary)
    return summary


def build_page_state(db: Session) -> dict[str, Any]:
    return {
        "module_options": module_options(),
        "history": list_history(db, limit=50),
        "import_jobs": list_jobs(db, limit=20),
        "background_threshold_rows": background_threshold_rows(db),
        "rollback_window_hours": rollback_window_hours(db),
        "format_options": ["csv", "json", "xlsx"],
        "csv_delimiter_options": [
            {"id": ",", "label": "Comma (,)"},
            {"id": ";", "label": "Semicolon (;)"},
            {"id": "\\t", "label": "Tab"},
            {"id": "|", "label": "Pipe (|)"},
        ],
    }


def csv_template(module: str) -> str:
    cfg = ENTITY_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported import module")
    headers = cfg["headers"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerow(["" for _ in headers])
    return buf.getvalue()


def module_headers(module: str) -> list[str]:
    cfg = ENTITY_CONFIG.get(module)
    if not cfg:
        raise ValueError("Unsupported import module")
    return list(cfg["headers"])


def rollback_import(
    db: Session,
    *,
    import_id: str,
    window_hours: int | None = None,
) -> dict[str, Any]:
    history = _history_entries(db)
    entry_idx = -1
    for idx, item in enumerate(history):
        if str(item.get("import_id") or "") == import_id:
            entry_idx = idx
            break
    if entry_idx < 0:
        raise ValueError("Import record not found")

    entry = history[entry_idx]
    from app.services.financial_imports import FINANCIAL_IMPORT_MODULES

    if str(entry.get("module") or "") in FINANCIAL_IMPORT_MODULES:
        raise ValueError(
            "Legacy financial and subscription import history cannot be raw-deleted. "
            "Payment batches with durable creation provenance use the Import Runs "
            "previewed batch-reversal owner; other financial modules use their "
            "named append-only lifecycle owners."
        )
    if bool(entry.get("dry_run")):
        raise ValueError("Dry-run imports cannot be rolled back")
    if entry.get("rolled_back_at"):
        raise ValueError("Import was already rolled back")
    if str(entry.get("status") or "") not in {"success", "partial"}:
        raise ValueError("Only successful or partial imports can be rolled back")

    timestamp_raw = str(entry.get("timestamp") or "")
    if not timestamp_raw:
        raise ValueError("Import timestamp is missing")
    try:
        imported_at = datetime.fromisoformat(timestamp_raw)
    except ValueError as exc:
        raise ValueError("Invalid import timestamp format") from exc
    if imported_at.tzinfo is None:
        imported_at = imported_at.replace(tzinfo=UTC)

    effective_window = window_hours or rollback_window_hours(db)
    expires_at = imported_at.timestamp() + (effective_window * 3600)
    now_ts = datetime.now(UTC).timestamp()
    if now_ts > expires_at:
        raise ValueError(f"Rollback window expired ({effective_window}h)")

    created_records = entry.get("created_records")
    if not isinstance(created_records, list) or not created_records:
        raise ValueError("No rollback metadata available for this import")

    deleted_rows = 0
    missing_rows = 0

    # Financial modules were rejected above. Legacy settings-history rollback is
    # intentionally limited to nonfinancial records that can still be removed.
    for record in created_records:
        if not isinstance(record, dict):
            continue
        module = str(record.get("module") or entry.get("module") or "")
        model_cls = ENTITY_MODEL_MAP.get(module)
        if model_cls is None:
            missing_rows += 1
            continue
        raw_id = str(record.get("id") or "").strip()
        if not raw_id:
            missing_rows += 1
            continue
        try:
            record_id = UUID(raw_id)
        except ValueError:
            missing_rows += 1
            continue

        obj = db.get(model_cls, record_id)
        if obj is None:
            missing_rows += 1
            continue

        db.delete(obj)
        deleted_rows += 1

    rolled_back_at = datetime.now(UTC).isoformat()
    entry["status"] = "rolled_back"
    entry["rolled_back_at"] = rolled_back_at
    entry["rolled_back_rows"] = deleted_rows
    entry["rollback_missing_rows"] = missing_rows
    history[entry_idx] = entry

    domain_settings_service.imports_settings.upsert_by_key(
        db,
        IMPORT_HISTORY_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=history[:200],
            value_text=None,
            is_secret=False,
        ),
    )
    db.flush()
    db.expire_all()
    return {
        "import_id": import_id,
        "rolled_back_rows": deleted_rows,
        "missing_rows": missing_rows,
        "window_hours": effective_window,
        "rolled_back_at": rolled_back_at,
    }
