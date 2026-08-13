from __future__ import annotations

import logging

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.imports.loader import load_csv_content
from app.models.domain_settings import SettingDomain
from app.schemas.imports import SubscriberCustomFieldImportRow
from app.schemas.subscriber import SubscriberCustomFieldCreate
from app.services import settings_spec
from app.services import subscriber as subscriber_service

logger = logging.getLogger(__name__)


def _imports_int_setting(db: Session, key: str) -> int:
    """Resolve an imports integer through its spec.

    Was a raw `DomainSetting` query with a caller-supplied fallback, which made
    `_DEFAULT_MAX_FILE_BYTES` / `_DEFAULT_MAX_ROWS` a second statement of
    defaults the specs already declare — and skipped the specs' bounds
    entirely. `control.settings_spec` owns the shape; this is a read through
    the owner (docs/SOT_RELATIONSHIP_MAP.md).
    """

    return settings_spec.resolve_integer(db, SettingDomain.imports, key)


def import_subscriber_custom_fields_from_csv(
    db: Session, content: str, max_rows: int | None = None
) -> tuple[int, list[dict[str, str | int]]]:
    created = 0
    errors: list[dict[str, str | int]] = []
    rows, row_errors = load_csv_content(
        content, SubscriberCustomFieldImportRow, max_rows=max_rows
    )
    errors.extend({"index": err.index, "detail": err.detail} for err in row_errors)
    for idx, import_row in rows:
        try:
            payload = SubscriberCustomFieldCreate(**import_row.model_dump())
            subscriber_service.subscriber_custom_fields.create(db, payload)
            created += 1
        except Exception as exc:
            db.rollback()
            errors.append({"index": idx, "detail": str(exc)})
    return created, errors


def import_subscriber_custom_fields_upload(
    db: Session, file: UploadFile
) -> dict[str, int | list[dict[str, str | int]]]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV file required")
    payload = file.file.read()
    max_file_bytes = _imports_int_setting(db, "max_file_bytes")
    if len(payload) > max_file_bytes:
        raise HTTPException(status_code=413, detail="CSV file too large")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid UTF-8 CSV content"
        ) from exc
    max_rows = _imports_int_setting(db, "max_rows")
    created, errors = import_subscriber_custom_fields_from_csv(
        db, content, max_rows=max_rows
    )
    if any(err.get("detail") == "Row limit exceeded" for err in errors):
        raise HTTPException(status_code=400, detail="CSV row limit exceeded")
    return {"created": created, "errors": errors, "error_count": len(errors)}
