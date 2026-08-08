"""Admin system management web routes."""

import hashlib
import json
import logging
import re
from base64 import b64encode
from datetime import UTC, datetime
from io import BytesIO
from typing import cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import qrcode
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import (
    billing as billing_service,
)
from app.services import branding_storage as branding_storage_service
from app.services import control_registry as control_registry_service
from app.services import email as email_service
from app.services import file_upload as file_upload_service
from app.services import import_runs as import_runs_service
from app.services import module_manager as module_manager_service
from app.services import nextcloud_talk_staff as nextcloud_talk_staff_service
from app.services import radius_reject as radius_reject_service
from app.services import rbac_catalog, settings_spec
from app.services import (
    scheduler as scheduler_service,
)
from app.services import session_manager as session_manager_service
from app.services import staff_provisioning as staff_provisioning_service
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import system_user_assignments as assignment_service
from app.services import web_control_plane as web_control_plane_service
from app.services import web_system_about as web_system_about_service
from app.services import web_system_api_key_forms as web_system_api_key_forms_service
from app.services import (
    web_system_api_key_mutations as web_system_api_key_mutations_service,
)
from app.services import web_system_api_keys as web_system_api_keys_service
from app.services import web_system_audit as web_system_audit_service
from app.services import web_system_billing_forms as web_system_billing_forms_service
from app.services import web_system_common as web_system_common_service
from app.services import web_system_company_info as web_system_company_info_service
from app.services import web_system_config as web_system_config_service
from app.services import web_system_db_inspector as web_system_db_inspector_service
from app.services import web_system_export_tool as web_system_export_tool_service
from app.services import web_system_form_views as web_system_form_views_service
from app.services import web_system_geocode_tool as web_system_geocode_tool_service
from app.services import web_system_health as web_system_health_service
from app.services import web_system_import_wizard as web_system_import_wizard_service
from app.services import web_system_overview as web_system_overview_service
from app.services import (
    web_system_permission_forms as web_system_permission_forms_service,
)
from app.services import web_system_profiles as web_system_profiles_service
from app.services import web_system_restore_tool as web_system_restore_tool_service
from app.services import web_system_role_forms as web_system_role_forms_service
from app.services import web_system_roles as web_system_roles_service
from app.services import web_system_scheduler as web_system_scheduler_service
from app.services import web_system_secrets as web_system_secrets_service
from app.services import web_system_settings_forms as web_system_settings_forms_service
from app.services import web_system_settings_hub as web_system_settings_hub_service
from app.services import web_system_settings_views as web_system_settings_views_service
from app.services import web_system_user_edit as web_system_user_edit_service
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services import web_system_users as web_system_users_service
from app.services.audit_helpers import (
    build_audit_activities,
    build_audit_activities_for_types,
    log_audit_event,
)
from app.services.auth_dependencies import load_permission_keys, require_permission
from app.services.brand_theme import (
    DEFAULT_HEX,
    DEFAULT_SECONDARY_HEX,
    is_accessible_semantic_color,
)
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.financial_import_batch_reversals import (
    PaymentImportBatchReversals,
)
from app.services.owner_commands import CommandContext
from app.tasks.exports import run_export_job
from app.tasks.gis import run_batch_geocode_job
from app.tasks.imports import (
    process_import_run as process_import_run_task,
)
from app.tasks.imports import run_import_job
from app.web.request_parsing import (
    parse_form_data,
    parse_form_data_sync,
    parse_json_body,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/system", tags=["web-admin-system"])
DB_INSPECTOR_COOKIE = "dbi_confirm"


def _system_page_context(request: Request, db: Session, title: str, active_page: str):
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": title,
        "heading": title,
        "description": f"Manage {title.lower()} configuration.",
        "empty_title": f"No {title.lower()} yet",
        "empty_message": "No records are available for this section.",
    }


def _dbi_principal_id(request: Request) -> str:
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    principal = str(
        current_user.get("subscriber_id") or current_user.get("id") or ""
    ).strip()
    if not principal:
        raise HTTPException(status_code=401, detail="Unable to resolve current user")
    return principal


def _require_system_user_principal(request: Request) -> dict:
    """Require that the current principal is a system user, not a subscriber."""
    auth = getattr(request.state, "auth", None) or {}
    if auth.get("principal_type") != "system_user":
        raise HTTPException(
            status_code=403,
            detail="This action requires a system user principal",
        )
    return auth


def _system_actor_id(request: Request) -> str | None:
    actor_id = getattr(request.state, "actor_id", None)
    if actor_id:
        return str(actor_id)
    auth = getattr(request.state, "auth", None) or {}
    principal_id = auth.get("principal_id")
    return str(principal_id) if principal_id else None


def _system_command_context(
    request: Request,
    *,
    reason: str,
    idempotency_key: str,
    scope: str = assignment_service.ASSIGNMENT_SCOPE,
) -> CommandContext:
    actor_id = _system_actor_id(request)
    if not actor_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    auth = getattr(request.state, "auth", None) or {}
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{actor_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _system_user_audit_snapshot(db: Session, user) -> dict[str, object]:
    return web_system_users_service.build_user_audit_snapshot(db, user)


def _diff_audit_snapshots(before: dict, after: dict) -> dict[str, dict[str, object]]:
    changes: dict[str, dict[str, object]] = {}
    for key in sorted(set(before) | set(after)):
        before_value = before.get(key)
        after_value = after.get(key)
        if before_value != after_value:
            changes[key] = {"from": before_value, "to": after_value}
    return changes


def _system_user_audit_items(db: Session, user_id: str, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(db, "system_user", str(user_id), limit=limit)
    except Exception:
        logger.exception("Unable to load system user audit items for %s", user_id)
        db.rollback()
        return []


def _log_system_user_event(
    db: Session,
    request: Request,
    *,
    action: str,
    user_id: str,
    metadata: dict | None = None,
) -> None:
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="system_user",
        entity_id=str(user_id),
        actor_id=_system_actor_id(request),
        metadata=metadata,
    )


def _billing_config_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(db, "billing_config", "billing", limit=limit)
    except Exception:
        logger.exception("Unable to load billing config audit items")
        db.rollback()
        return []


def _direct_bank_transfer_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(
            db, "direct_bank_transfer_config", "direct_bank_transfer", limit=limit
        )
    except Exception:
        logger.exception("Unable to load direct bank transfer audit items")
        db.rollback()
        return []


def _billing_reminder_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(
            db, "billing_reminder_config", "reminders", limit=limit
        )
    except Exception:
        logger.exception("Unable to load billing reminder audit items")
        db.rollback()
        return []


def _billing_notification_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(
            db, "billing_notification_config", "billing_notifications", limit=limit
        )
    except Exception:
        logger.exception("Unable to load billing notification audit items")
        db.rollback()
        return []


def _tax_rate_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities_for_types(db, ["tax_rate"], limit=limit)
    except Exception:
        logger.exception("Unable to load tax rate audit items")
        db.rollback()
        return []


def _plan_change_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(
            db, "plan_change_config", "plan_change", limit=limit
        )
    except Exception:
        logger.exception("Unable to load plan change audit items")
        db.rollback()
        return []


def _radius_config_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities(db, "radius_config", "radius", limit=limit)
    except Exception:
        logger.exception("Unable to load RADIUS config audit items")
        db.rollback()
        return []


@router.get("/health", response_class=HTMLResponse)
def system_health_page(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_health_service.build_health_data(db)
    context = {
        "request": request,
        "active_page": "system-health",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/health.html", context)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_overview(request: Request, db: Session = Depends(get_db)):
    """System settings overview."""
    from app.web.admin import get_current_user, get_sidebar_stats

    dashboard = web_system_overview_service.get_dashboard_stats(db)
    return templates.TemplateResponse(
        "admin/system/index.html",
        {
            "request": request,
            "active_page": "system",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **dashboard,
        },
    )


@router.get(
    "/modules",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def modules_manager_page(request: Request, db: Session = Depends(get_db)):
    """Main module manager with module and feature toggles."""
    from app.web.admin import get_current_user, get_sidebar_stats

    state = module_manager_service.module_manager_page_state(db)
    return templates.TemplateResponse(
        "admin/system/modules.html",
        {
            "request": request,
            "active_page": "system-modules",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "save_success": request.query_params.get("saved") == "1",
            **state,
        },
    )


@router.post(
    "/modules", dependencies=[Depends(require_permission("system:settings:write"))]
)
def modules_manager_save(request: Request, db: Session = Depends(get_db)):
    """Persist module manager toggles."""
    from app.services.control_registry import Layer

    form = parse_form_data_sync(request)
    module_payload: dict[str, bool] = {}
    feature_payload: dict[str, bool | None] = {}

    for module_name in module_manager_service.MODULE_KEY_MAP:
        field_name = f"module__{module_name}"
        if field_name in form:
            module_payload[module_name] = (
                str(form.get(field_name) or "").lower() == "true"
            )

    stored_values: dict[str, bool | None] = {
        "inherit": None,
        "on": True,
        "off": False,
    }
    for control in control_registry_service.all_controls():
        if control.layer is not Layer.feature:
            continue
        field_name = f"control__{control.key}"
        if field_name not in form:
            continue
        raw_value = str(form.get(field_name) or "").lower()
        if raw_value not in stored_values:
            raise HTTPException(status_code=400, detail="Invalid feature control value")
        feature_payload[control.key] = stored_values[raw_value]

    provider_payload: dict[str, bool] = {}
    for provider in module_manager_service.list_payment_providers(db):
        provider_id = provider["id"]
        field_name = f"provider__{provider_id}"
        if field_name in form:
            provider_payload[provider_id] = (
                str(form.get(field_name) or "").lower() == "true"
            )

    from app.services.control_relationships import (
        ControlRelationshipError,
        validate_feature_control_changes,
    )

    try:
        validate_feature_control_changes(db, feature_payload)
    except ControlRelationshipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    module_manager_service.update_module_flags(db, payload=module_payload)
    feature_changes = control_registry_service.update_canonical_feature_controls(
        db, payload=feature_payload
    )
    module_manager_service.update_provider_flags(db, payload=provider_payload)
    if feature_changes:
        log_audit_event(
            db=db,
            request=request,
            action="feature_controls.update",
            entity_type="feature_control",
            entity_id="canonical-features",
            actor_id=_system_actor_id(request),
            metadata={"changes": feature_changes},
        )
    return RedirectResponse("/admin/system/modules?saved=1", status_code=303)


@router.get(
    "/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_wizard(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_import_wizard_service.build_page_state(db)
    rollback_notice = request.query_params.get("rollback_notice")
    rollback_error = request.query_params.get("rollback_error")
    job_notice = request.query_params.get("job_notice")
    active_job_id = request.query_params.get("job_id")
    active_job = (
        web_system_import_wizard_service.get_job(db, active_job_id)
        if active_job_id
        else None
    )
    return templates.TemplateResponse(
        "admin/system/import_wizard.html",
        {
            "request": request,
            "active_page": "system-import",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "rollback_notice": rollback_notice,
            "rollback_error": rollback_error,
            "job_notice": job_notice,
            "active_job_id": active_job_id,
            "active_job": active_job,
            **state,
        },
    )


@router.get(
    "/import/template.csv",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_template_csv(
    module: str = Query(...),
):
    from fastapi.responses import StreamingResponse

    content = web_system_import_wizard_service.csv_template(module)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="import_template_{module}.csv"'
        },
    )


@router.post(
    "/import/mapping-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_mapping_preview(
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    data_format = str(form.get("data_format") or "csv").strip().lower()
    payload_text = str(form.get("payload_text") or "")
    csv_delimiter = str(form.get("csv_delimiter") or ",")
    payload_file = cast(UploadFile | None, form.get("payload_file"))
    file_bytes: bytes | None = None
    if payload_file is not None and payload_file.filename:
        file_bytes = payload_file.file.read()

    state = web_system_import_wizard_service.detect_columns_and_preview(
        data_format=data_format,
        raw_text=payload_text,
        csv_delimiter=csv_delimiter,
        file_bytes=file_bytes,
    )
    headers = web_system_import_wizard_service.module_headers(module)
    return templates.TemplateResponse(
        "admin/system/_import_mapping.html",
        {
            "request": request,
            "module": module,
            "target_headers": headers,
            "selected_delimiter": csv_delimiter,
            **state,
        },
    )


@router.post(
    "/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_wizard_submit(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "").strip()
    data_format = str(form.get("data_format") or "csv").strip().lower()
    csv_delimiter = str(form.get("csv_delimiter") or ",")
    payload_text = str(form.get("payload_text") or "")
    source_name = str(form.get("source_name") or "manual")
    payload_file = cast(UploadFile | None, form.get("payload_file"))
    file_bytes: bytes | None = None
    if payload_file is not None and payload_file.filename:
        source_name = (
            source_name if source_name.strip() != "manual" else payload_file.filename
        )
        file_bytes = payload_file.file.read()
    dry_run = form.get("dry_run")
    column_mapping: dict[str, str] = {}
    for key, value in form.items():
        if str(key).startswith("mapping__"):
            source_col = str(key).split("mapping__", 1)[1]
            target_col = str(value or "").strip()
            if source_col:
                column_mapping[source_col] = target_col

    try:
        parsed_preview = web_system_import_wizard_service.parse_payload(
            data_format=data_format,
            raw_text=payload_text,
            source_name=source_name,
            csv_delimiter=csv_delimiter,
            file_bytes=file_bytes,
        )
        total_rows = len(parsed_preview.rows)
        threshold = web_system_import_wizard_service.background_threshold_rows(db)
        from app.services.financial_imports import FINANCIAL_IMPORT_MODULES

        if module in FINANCIAL_IMPORT_MODULES:
            # Financial input always enters the durable run ledger first. Files
            # are normalized to JSON so the exact validated rows are retained
            # and the apply action cannot read changed upload bytes.
            staged_text = json.dumps(parsed_preview.rows, default=str)
            run = import_runs_service.create_import_run(
                db,
                module=module,
                raw_text=staged_text,
                data_format="json",
                source_name=source_name,
                column_mapping=column_mapping,
                csv_delimiter=csv_delimiter,
                dry_run=True,
                created_by=(current_user.get("email") or "").strip() or None,
            )
            if total_rows >= threshold:
                from app.services.queue_adapter import enqueue_task

                enqueue_task(
                    process_import_run_task,
                    args=[str(run.id)],
                    correlation_id=f"financial_import:{run.id}",
                    source="admin_system_import",
                    request_id=getattr(request.state, "request_id", None),
                    actor_id=str(current_user.get("subscriber_id") or "").strip()
                    or None,
                )
            else:
                import_runs_service.process_import_run(db, run.id)
            return RedirectResponse(
                f"/admin/system/import-runs/{run.id}", status_code=303
            )

        if total_rows >= threshold:
            job_id = str(uuid4())
            web_system_import_wizard_service.upsert_job(
                db,
                {
                    "job_id": job_id,
                    "module": module,
                    "module_label": web_system_import_wizard_service.ENTITY_CONFIG.get(
                        module, {}
                    ).get("label", module),
                    "source_name": source_name,
                    "status": "queued",
                    "queued_at": datetime.now(UTC).isoformat(),
                    "progress_percent": 0,
                    "row_count": total_rows,
                    "threshold_rows": threshold,
                    "requested_by": current_user.get("email") or "",
                    "result": None,
                    "error": None,
                },
            )
            from app.services.queue_adapter import enqueue_task

            enqueue_task(
                run_import_job,
                kwargs={
                    "job_id": job_id,
                    "module": module,
                    "data_format": data_format,
                    "raw_text": payload_text,
                    "source_name": source_name,
                    "dry_run": dry_run is not None,
                    "column_mapping": column_mapping,
                    "csv_delimiter": csv_delimiter,
                    "file_bytes_b64": b64encode(file_bytes).decode("ascii")
                    if file_bytes is not None
                    else None,
                    "notify_email": (current_user.get("email") or "").strip() or None,
                },
                correlation_id=f"system_import:{job_id}",
                source="admin_system_import",
                request_id=getattr(request.state, "request_id", None),
                actor_id=str(current_user.get("subscriber_id") or "").strip() or None,
            )
            notice = quote_plus(
                f"Large import queued in background ({total_rows} rows). Track progress below."
            )
            return RedirectResponse(
                f"/admin/system/import?job_id={job_id}&job_notice={notice}",
                status_code=303,
            )

        result = web_system_import_wizard_service.execute_import(
            db,
            module=module,
            data_format=data_format,
            raw_text=payload_text,
            source_name=source_name,
            dry_run=dry_run is not None,
            column_mapping=column_mapping,
            csv_delimiter=csv_delimiter,
            file_bytes=file_bytes,
        )
        state = web_system_import_wizard_service.build_page_state(db)
        return templates.TemplateResponse(
            "admin/system/import_wizard.html",
            {
                "request": request,
                "active_page": "system-import",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "result": result,
                "form": {
                    "module": module,
                    "data_format": data_format,
                    "csv_delimiter": csv_delimiter,
                    "payload_text": payload_text,
                    "source_name": source_name,
                    "dry_run": dry_run is not None,
                },
                **state,
            },
            status_code=200
            if result.get("status") in {"success", "dry_run", "partial"}
            else 400,
        )
    except Exception as exc:
        state = web_system_import_wizard_service.build_page_state(db)
        return templates.TemplateResponse(
            "admin/system/import_wizard.html",
            {
                "request": request,
                "active_page": "system-import",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "error": str(exc),
                "form": {
                    "module": module,
                    "data_format": data_format,
                    "csv_delimiter": csv_delimiter,
                    "payload_text": payload_text,
                    "source_name": source_name,
                    "dry_run": dry_run is not None,
                },
                **state,
            },
            status_code=400,
        )


@router.get(
    "/import/jobs/{job_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_job_status(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
):
    job = web_system_import_wizard_service.get_job(db, job_id)
    return templates.TemplateResponse(
        "admin/system/_import_job_status.html",
        {
            "request": request,
            "job": job,
        },
    )


@router.get(
    "/import-runs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_runs(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    runs = import_runs_service.list_import_runs(db, limit=100)
    return templates.TemplateResponse(
        "admin/system/import_runs.html",
        {
            "request": request,
            "active_page": "system-import",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "runs": runs,
        },
    )


@router.get(
    "/import-runs/{run_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_import_run_detail(
    request: Request, run_id: str, db: Session = Depends(get_db)
):
    from app.web.admin import get_current_user, get_sidebar_stats

    run = import_runs_service.get_import_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/system/import-runs", status_code=303)
    batch_reversal_capability = PaymentImportBatchReversals.capability(db, run.id)
    return templates.TemplateResponse(
        "admin/system/import_run_detail.html",
        {
            "request": request,
            "active_page": "system-import",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "run": run,
            "rows": run.rows,
            "batch_reversal": run.payment_batch_reversal,
            "batch_reversal_capability": batch_reversal_capability,
            "reversal_error": request.query_params.get("reversal_error"),
            "reversal_notice": request.query_params.get("reversal_notice"),
        },
    )


@router.post(
    "/import-runs/{run_id}/apply",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_run_apply(
    request: Request, run_id: str, db: Session = Depends(get_db)
):
    from app.web.admin import get_current_user

    user = get_current_user(request)
    try:
        applied = import_runs_service.apply_from_dry_run(
            db, run_id, created_by=(user.get("email") or "").strip() or None
        )
    except ValueError:
        return RedirectResponse(f"/admin/system/import-runs/{run_id}", status_code=303)
    return RedirectResponse(f"/admin/system/import-runs/{applied.id}", status_code=303)


@router.post(
    "/import-runs/{run_id}/payment-reversal-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_payment_reversal_preview(
    request: Request, run_id: str, db: Session = Depends(get_db)
):
    from app.web.admin import get_current_user, get_sidebar_stats

    form = parse_form_data_sync(request)
    reason = str(form.get("reason") or "").strip()
    try:
        preview = PaymentImportBatchReversals.preview(db, run_id, reason=reason)
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return RedirectResponse(
            f"/admin/system/import-runs/{run_id}?reversal_error={quote_plus(str(detail))}",
            status_code=303,
        )
    return templates.TemplateResponse(
        "admin/system/import_payment_batch_reversal_confirm.html",
        {
            "request": request,
            "active_page": "system-import",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "preview": preview,
            "preview_snapshot": preview.as_snapshot(),
            "preview_fingerprint": preview.fingerprint,
            "idempotency_key": f"payment-import-batch-reversal:{preview.import_run_id}",
        },
    )


@router.post(
    "/import-runs/{run_id}/payment-reversal-confirm",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_payment_reversal_confirm(
    request: Request, run_id: str, db: Session = Depends(get_db)
):
    from app.web.admin import get_current_user

    form = parse_form_data_sync(request)
    user = get_current_user(request)
    try:
        result = PaymentImportBatchReversals.confirm(
            db,
            run_id,
            reason=str(form.get("reason") or ""),
            preview_fingerprint=str(form.get("preview_fingerprint") or ""),
            idempotency_key=str(form.get("idempotency_key") or ""),
            actor_id=(user.get("email") or "").strip() or None,
        )
    except (HTTPException, ValueError) as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return RedirectResponse(
            f"/admin/system/import-runs/{run_id}?reversal_error={quote_plus(str(detail))}",
            status_code=303,
        )
    replay = " Existing evidence was returned." if result.idempotent_replay else ""
    notice = quote_plus(
        f"Reversed {result.batch_reversal.reversed_payment_count} imported payments "
        f"through exact append-only evidence.{replay}"
    )
    return RedirectResponse(
        f"/admin/system/import-runs/{run_id}?reversal_notice={notice}",
        status_code=303,
    )


@router.get(
    "/export",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_tool(
    request: Request,
    module: str | None = Query(None),
    template: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    selected_module = (module or "subscribers").strip()
    selected_template_id = (template or "").strip() or None
    selected_fields: list[str] | None = None
    selected_delimiter = ","
    selected_export_format = "csv"
    selected_date_from: str | None = None
    selected_date_to: str | None = None
    selected_status: str | None = None
    selected_include_headers = True

    if selected_template_id:
        template_data = web_system_export_tool_service.get_export_template(
            db, selected_template_id
        )
        if template_data:
            config = (
                template_data.get("config")
                if isinstance(template_data.get("config"), dict)
                else {}
            )
            selected_module = str(config.get("module") or selected_module).strip()
            selected_fields = [
                str(field) for field in config.get("selected_fields") or []
            ]
            selected_delimiter = str(config.get("delimiter") or ",")
            selected_export_format = str(config.get("export_format") or "csv")
            selected_date_from = str(config.get("date_from") or "").strip() or None
            selected_date_to = str(config.get("date_to") or "").strip() or None
            selected_status = str(config.get("status") or "").strip() or None
            selected_include_headers = bool(config.get("include_headers", True))

    try:
        available_fields = web_system_export_tool_service.module_fields(selected_module)
        status_options = web_system_export_tool_service.module_status_options(
            selected_module
        )
    except Exception:
        selected_module = "subscribers"
        available_fields = web_system_export_tool_service.module_fields(selected_module)
        status_options = web_system_export_tool_service.module_status_options(
            selected_module
        )

    return templates.TemplateResponse(
        "admin/system/export_tool.html",
        {
            "request": request,
            "active_page": "system-export",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "module_options": web_system_export_tool_service.module_options(),
            "delimiter_options": web_system_export_tool_service.DELIMITER_OPTIONS,
            "export_format_options": web_system_export_tool_service.EXPORT_FORMAT_OPTIONS,
            "export_templates": web_system_export_tool_service.list_export_templates(
                db
            ),
            "selected_template_id": selected_template_id,
            "selected_module": selected_module,
            "available_fields": available_fields,
            "status_options": status_options,
            "frequency_options": web_system_export_tool_service.SCHEDULE_FREQUENCY_OPTIONS,
            "export_schedules": web_system_export_tool_service.list_export_schedules(
                db
            ),
            "export_jobs": web_system_export_tool_service.list_export_jobs(
                db, limit=20
            ),
            "selected_fields": selected_fields or available_fields,
            "selected_delimiter": selected_delimiter,
            "selected_export_format": selected_export_format,
            "selected_date_from": selected_date_from,
            "selected_date_to": selected_date_to,
            "selected_status": selected_status,
            "selected_include_headers": selected_include_headers,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
        },
    )


@router.post(
    "/export/download",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_download(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import StreamingResponse

    from app.web.admin import get_current_user

    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    selected_fields = [
        str(field).strip() for field in form.getlist("fields") if str(field).strip()
    ]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv")
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    current_user = get_current_user(request)
    requester_email = str(current_user.get("email") or "").strip() or None
    actor_id = str(current_user.get("person_id") or "").strip() or None

    try:
        row_count = web_system_export_tool_service.count_rows(
            db,
            module=module,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )
        if row_count > web_system_export_tool_service.EXPORT_BG_THRESHOLD_ROWS:
            job = web_system_export_tool_service.create_export_job(
                db,
                module=module,
                selected_fields=selected_fields,
                delimiter=delimiter,
                export_format=export_format,
                date_from=date_from,
                date_to=date_to,
                status=status,
                include_headers=include_headers,
                recipient_email=requester_email,
                requested_by_email=requester_email,
                row_count=row_count,
            )
            from app.services.queue_adapter import enqueue_task

            enqueue_task(
                run_export_job,
                kwargs={"job_id": str(job["id"])},
                correlation_id=f"system_export:{job['id']}",
                source="admin_system_export",
                request_id=getattr(request.state, "request_id", None),
                actor_id=actor_id,
            )
            web_system_export_tool_service.log_export_audit_event(
                db,
                action="export_job_queued",
                module=module,
                actor_id=actor_id,
                actor_type=AuditActorType.user if actor_id else AuditActorType.system,
                entity_type="export_job",
                entity_id=str(job["id"]),
                metadata={
                    "row_count": row_count,
                    "format": export_format,
                },
            )
            notice = quote_plus(
                f"Large export queued ({row_count} rows). Download link will be emailed when ready."
            )
            return RedirectResponse(
                f"/admin/system/export?module={quote_plus(module)}&notice={notice}",
                status_code=303,
            )

        content, media_type, extension, row_count = (
            web_system_export_tool_service.export_content(
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
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        filename = f"export_{module}_{row_count}_{timestamp}.{extension}"
        web_system_export_tool_service.log_export_audit_event(
            db,
            action="export_download",
            module=module,
            actor_id=actor_id,
            actor_type=AuditActorType.user if actor_id else AuditActorType.system,
            entity_type="system_export",
            entity_id=None,
            metadata={
                "row_count": row_count,
                "format": export_format,
                "filename": filename,
            },
        )
        return StreamingResponse(
            iter([content]),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        module_q = quote_plus(module)
        return RedirectResponse(
            f"/admin/system/export?module={module_q}&error={error}", status_code=303
        )


@router.get(
    "/export/jobs/{job_id}/download",
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def system_export_job_download(
    request: Request, job_id: str, db: Session = Depends(get_db)
):
    from fastapi.responses import FileResponse

    from app.web.admin import get_current_user

    job = web_system_export_tool_service.get_export_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if str(job.get("status") or "") != "completed":
        raise HTTPException(status_code=409, detail="Export job is not completed")
    file_path = str(job.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=404, detail="Export file not available")
    filename = str(job.get("filename") or f"export_{job_id}.dat")
    current_user = get_current_user(request)
    actor_id = str(current_user.get("person_id") or "").strip() or None
    web_system_export_tool_service.log_export_audit_event(
        db,
        action="export_job_download",
        module=str(job.get("module") or ""),
        actor_id=actor_id,
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        entity_type="export_job",
        entity_id=job_id,
        metadata={"filename": filename},
    )
    return FileResponse(
        path=file_path, filename=filename, media_type="application/octet-stream"
    )


@router.post(
    "/export/templates",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_create_template(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    template_name = str(form.get("template_name") or "").strip()
    selected_fields = [
        str(field).strip() for field in form.getlist("fields") if str(field).strip()
    ]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv")
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    try:
        template_data = web_system_export_tool_service.create_export_template(
            db,
            name=template_name,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            export_format=export_format,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
        )
        notice = quote_plus("Export template saved.")
        template_id = quote_plus(str(template_data.get("id") or ""))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&template={template_id}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&error={error}",
            status_code=303,
        )


@router.post(
    "/export/templates/{template_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_delete_template(
    template_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    try:
        web_system_export_tool_service.delete_export_template(
            db, template_id=template_id
        )
        notice = quote_plus("Export template removed.")
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&error={error}",
            status_code=303,
        )


@router.post(
    "/export/schedules",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_create_schedule(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    selected_fields = [
        str(field).strip() for field in form.getlist("fields") if str(field).strip()
    ]
    delimiter = str(form.get("delimiter") or ",")
    export_format = str(form.get("export_format") or "csv").strip().lower()
    date_from = str(form.get("date_from") or "").strip() or None
    date_to = str(form.get("date_to") or "").strip() or None
    status = str(form.get("status") or "").strip() or None
    include_headers = form.get("include_headers") is not None
    name = str(form.get("schedule_name") or "").strip()
    recipient_email = str(form.get("recipient_email") or "").strip()
    frequency = str(form.get("frequency") or "weekly").strip().lower()
    custom_interval_raw = str(form.get("custom_interval_hours") or "").strip()
    custom_interval_hours = (
        int(custom_interval_raw) if custom_interval_raw.isdigit() else None
    )

    try:
        web_system_export_tool_service.create_export_schedule(
            db,
            name=name,
            module=module,
            selected_fields=selected_fields,
            delimiter=delimiter,
            export_format=export_format,
            date_from=date_from,
            date_to=date_to,
            status=status,
            include_headers=include_headers,
            recipient_email=recipient_email,
            frequency=frequency,
            custom_interval_hours=custom_interval_hours,
        )
        notice = quote_plus("Scheduled export created.")
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&error={error}",
            status_code=303,
        )


@router.post(
    "/export/schedules/{schedule_id}/toggle",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_toggle_schedule(
    schedule_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    enabled = str(form.get("enabled") or "").strip() == "1"
    try:
        web_system_export_tool_service.set_export_schedule_enabled(
            db,
            schedule_id=schedule_id,
            enabled=enabled,
        )
        notice = quote_plus("Scheduled export updated.")
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&error={error}",
            status_code=303,
        )


@router.post(
    "/export/schedules/{schedule_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_export_delete_schedule(
    schedule_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    module = str(form.get("module") or "subscribers").strip()
    try:
        web_system_export_tool_service.delete_export_schedule(
            db, schedule_id=schedule_id
        )
        notice = quote_plus("Scheduled export removed.")
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/export?module={quote_plus(module)}&error={error}",
            status_code=303,
        )


@router.post(
    "/import/{import_id}/rollback",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def system_import_wizard_rollback(
    import_id: str,
    db: Session = Depends(get_db),
):
    try:
        result = web_system_import_wizard_service.rollback_import(
            db, import_id=import_id
        )
        notice = quote_plus(
            f"Import {import_id} rolled back: {result['rolled_back_rows']} rows removed."
        )
        return RedirectResponse(
            f"/admin/system/import?rollback_notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/system/import?rollback_error={error}",
            status_code=303,
        )


@router.get(
    "/users",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def users_list(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(500, ge=10, le=500),
    offset: int | None = Query(None, ge=0),
    limit: int | None = Query(None, ge=5, le=500),
    db: Session = Depends(get_db),
):
    """List system users."""
    if limit is None:
        limit = per_page
    if offset is None:
        offset = (page - 1) * limit

    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/users/_table_content.html",
            {
                "request": request,
                **state,
                "htmx_url": "/admin/system/users/filter",
                "htmx_target": "users-table-content",
            },
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/users/index.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "user_type_options": state["user_type_options"],
        },
    )


@router.get(
    "/users/search",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def users_search(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=5, le=500),
    db: Session = Depends(get_db),
):
    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return templates.TemplateResponse(
        "admin/system/users/_table_content.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
        },
    )


@router.get(
    "/users/filter",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def users_filter(
    request: Request,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    filters: str | None = None,
    order_by: str | None = Query("last_name"),
    order_dir: str | None = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=5, le=500),
    db: Session = Depends(get_db),
):
    state = web_system_users_service.build_users_page_state(
        db,
        search=search,
        role=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return templates.TemplateResponse(
        "admin/system/users/_table_content.html",
        {
            "request": request,
            **state,
            "htmx_url": "/admin/system/users/filter",
            "htmx_target": "users-table-content",
        },
    )


@router.get("/users/profile", response_class=HTMLResponse)
def user_profile(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    auth = getattr(request.state, "auth", None) or {}
    system_user_id = (
        auth.get("principal_id")
        if auth.get("principal_type") == "system_user"
        else None
    )
    success = None
    if request.query_params.get("mfa") == "enabled":
        success = "Two-factor authentication enabled successfully."
    elif request.query_params.get("sessions") == "signed-out":
        success = "Other active sessions signed out."
    elif request.query_params.get("device_login") == "updated":
        success = "Router device login updated."
    state = web_system_profiles_service.build_profile_page_state(
        db,
        current_user=current_user,
        success=success,
        system_user_id=system_user_id,
        current_session_id=auth.get("session_id"),
    )

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/profile.html", context)


def _user_profile_template_response(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    auth = getattr(request.state, "auth", None) or {}
    system_user_id = (
        auth.get("principal_id")
        if auth.get("principal_type") == "system_user"
        else None
    )
    state = web_system_profiles_service.build_profile_page_state(
        db,
        current_user=current_user,
        error=error,
        success=success,
        system_user_id=system_user_id,
        current_session_id=auth.get("session_id"),
    )
    return templates.TemplateResponse(
        "admin/system/profile.html",
        {
            "request": request,
            "active_page": "users",
            "active_menu": "system",
            "current_user": current_user,
            "sidebar_stats": get_sidebar_stats(db),
            **state,
        },
        status_code=status_code,
    )


@router.post("/users/profile", response_class=HTMLResponse)
def user_profile_update(
    request: Request,
    first_name: str = Form(None),
    last_name: str = Form(None),
    email: str = Form(None),
    phone: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    auth = getattr(request.state, "auth", None) or {}
    system_user_id = (
        auth.get("principal_id")
        if auth.get("principal_type") == "system_user"
        else None
    )
    error = None
    success = None
    updated_person_id = None

    if system_user_id:
        try:
            target_id = UUID(str(system_user_id))
            values = {
                staff_provisioning_service.StaffIdentityField.first_name: first_name,
                staff_provisioning_service.StaffIdentityField.last_name: last_name,
                staff_provisioning_service.StaffIdentityField.email: email,
                staff_provisioning_service.StaffIdentityField.phone: phone,
            }
            fields = frozenset(
                field for field, value in values.items() if value is not None
            )
            if fields:
                desired_fingerprint = hashlib.sha256(
                    "\x1f".join(
                        f"{field.value}={values[field] or ''}"
                        for field in sorted(fields, key=lambda item: item.value)
                    ).encode()
                ).hexdigest()
                outcome = staff_provisioning_service.update_staff_identity(
                    db,
                    staff_provisioning_service.UpdateStaffIdentityCommand(
                        context=_system_command_context(
                            request,
                            reason="Self-service staff profile update",
                            idempotency_key=(
                                f"staff-self-profile:{target_id}:{desired_fingerprint}"
                            ),
                            scope=staff_provisioning_service.STAFF_PROFILE_SCOPE,
                        ),
                        user_id=target_id,
                        fields=fields,
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        phone=phone,
                    ),
                )
                updated_person_id = outcome.user_id
            success = "Profile updated successfully."
        except (DomainError, ValueError) as exc:
            error = exc.message if isinstance(exc, DomainError) else str(exc)
    state = web_system_profiles_service.build_profile_page_state(
        db,
        current_user=current_user,
        error=error,
        success=success,
        person_id=updated_person_id,
        system_user_id=system_user_id if updated_person_id is None else None,
        current_session_id=auth.get("session_id"),
    )

    context = {
        "request": request,
        "active_page": "users",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        **state,
    }
    return templates.TemplateResponse("admin/system/profile.html", context)


@router.post("/users/profile/device-login", response_class=HTMLResponse)
def user_profile_device_login(
    request: Request,
    form_data=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Allow router admins to set or rotate their own router RADIUS secret."""
    from app.tasks.radius_population import sync_device_login

    auth = _require_system_user_principal(request)
    principal_id = str(auth.get("principal_id") or "")
    if not principal_id:
        raise HTTPException(status_code=401, detail="Unable to resolve current user")

    person = web_system_profiles_service.get_subscriber(db, principal_id)
    if not person:
        raise HTTPException(status_code=404, detail="User not found")

    device_login_state = web_system_profiles_service.get_device_login_state(db, person)
    if not device_login_state["device_login_eligible"]:
        raise HTTPException(
            status_code=403,
            detail="Router device login is only available to router admins",
        )

    action = str(form_data.get("device_login_action") or "set")
    current_password = str(form_data.get("current_password") or "")
    if not web_system_profiles_service.verify_current_password(
        db, system_user_id=person.id, password=current_password
    ):
        return _user_profile_template_response(
            request,
            db,
            error="Current password is incorrect.",
            status_code=400,
        )

    try:
        if action == "revoke":
            web_system_user_mutations_service.revoke_device_login(
                db, user_id=str(person.id)
            )
            note = "Router device login revoked."
        elif action == "set":
            secret = str(form_data.get("device_login_secret") or "")
            confirm = str(form_data.get("device_login_secret_confirm") or "")
            if len(secret) < 12:
                raise ValueError("Router password must be at least 12 characters.")
            if not any(ch.isalpha() for ch in secret):
                raise ValueError("Router password must include at least one letter.")
            if not any(ch.isdigit() for ch in secret):
                raise ValueError("Router password must include at least one number.")
            if not any(not ch.isalnum() for ch in secret):
                raise ValueError("Router password must include at least one symbol.")
            if secret != confirm:
                raise ValueError("Router password confirmation does not match.")
            web_system_user_mutations_service.set_device_login(
                db, user_id=str(person.id), enabled=True, secret=secret
            )
            note = "Router device login updated."
        else:
            raise ValueError("Unsupported device-login action.")

        _log_system_user_event(
            db,
            request,
            action=f"device_login_self_{action}",
            user_id=str(person.id),
            metadata={"action": action, "source": "profile"},
        )
        sync_device_login.delay()
    except ValueError as exc:
        db.rollback()
        return _user_profile_template_response(
            request,
            db,
            error=str(exc),
            status_code=400,
        )
    except Exception:
        db.rollback()
        return _user_profile_template_response(
            request,
            db,
            error="Router device-login update failed.",
            status_code=500,
        )

    return RedirectResponse(
        url="/admin/system/users/profile?device_login=updated",
        status_code=303,
        headers={"HX-Trigger": json.dumps({"showToast": {"message": note}})},
    )


@router.post("/users/profile/sessions/sign-out-others")
def user_profile_sign_out_other_sessions(
    request: Request,
    db: Session = Depends(get_db),
):
    auth = _require_system_user_principal(request)
    principal_id = str(auth.get("principal_id") or "")
    if not principal_id:
        return RedirectResponse(url="/admin/system/users/profile", status_code=303)

    session_manager_service.revoke_all_other_sessions(
        db,
        coerce_uuid(principal_id),
        str(auth.get("session_id") or ""),
        principal_type="system_user",
    )
    return RedirectResponse(
        url="/admin/system/users/profile?sessions=signed-out", status_code=303
    )


@router.get("/users/profile/mfa/setup", response_class=HTMLResponse)
def user_profile_mfa_setup(request: Request, db: Session = Depends(get_db)):
    from app.services import auth_flow as auth_flow_service
    from app.web.admin import get_current_user, get_sidebar_stats

    auth = _require_system_user_principal(request)
    principal_id = str(auth.get("principal_id") or "")
    if not principal_id:
        return RedirectResponse(url="/admin/system/users/profile", status_code=303)

    setup = auth_flow_service.auth_flow.admin_mfa_setup(
        db, principal_id, "Authenticator app"
    )
    otpauth_uri = str(setup["otpauth_uri"])
    return templates.TemplateResponse(
        "admin/system/profile_mfa_setup.html",
        {
            "request": request,
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "method_id": setup["method_id"],
            "secret_key": setup["secret"],
            "otpauth_uri": otpauth_uri,
            "qr_code_url": _qr_code_data_url(otpauth_uri),
        },
    )


@router.post("/users/profile/mfa/setup", response_class=HTMLResponse)
def user_profile_mfa_setup_post(request: Request, db: Session = Depends(get_db)):
    return user_profile_mfa_setup(request, db)


@router.post("/users/profile/mfa/confirm", response_class=HTMLResponse)
def user_profile_mfa_confirm(
    request: Request,
    method_id: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.services import auth_flow as auth_flow_service
    from app.web.admin import get_current_user, get_sidebar_stats

    auth = _require_system_user_principal(request)
    principal_id = str(auth.get("principal_id") or "")
    if not principal_id:
        return RedirectResponse(url="/admin/system/users/profile", status_code=303)

    try:
        method = auth_flow_service.auth_flow.admin_mfa_confirm(
            db, method_id, code.strip(), principal_id
        )
    except Exception:
        return templates.TemplateResponse(
            "admin/system/profile_mfa_setup.html",
            {
                "request": request,
                "active_page": "users",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "qr_code_url": "",
                "error": "Invalid verification code. Please try again.",
            },
            status_code=401,
        )

    recovery_codes = (
        auth_flow_service.generate_mfa_recovery_codes(db, method)
        if getattr(method, "id", None)
        else []
    )
    if recovery_codes:
        return templates.TemplateResponse(
            "admin/system/profile_mfa_setup.html",
            {
                "request": request,
                "active_page": "users",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "qr_code_url": "",
                "recovery_codes": recovery_codes,
                "continue_url": "/admin/system/users/profile?mfa=enabled",
            },
        )

    return RedirectResponse(
        url="/admin/system/users/profile?mfa=enabled", status_code=303
    )


@router.post(
    "/users/bulk/user-type", dependencies=[Depends(require_permission("rbac:assign"))]
)
def users_bulk_set_user_type(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    user_type = data.get("user_type")
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")
    if not user_type or not isinstance(user_type, str):
        raise HTTPException(status_code=400, detail="user_type is required")

    updated = web_system_user_mutations_service.bulk_set_user_type(
        db,
        user_ids=[str(item) for item in user_ids],
        user_type=user_type,
    )
    return {
        "message": f"Updated user type for {updated} users.",
        "updated_count": updated,
    }


@router.post(
    "/users/bulk/delete", dependencies=[Depends(require_permission("rbac:assign"))]
)
def users_bulk_delete(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")

    deleted, skipped = web_system_user_mutations_service.bulk_delete_user_records(
        db,
        user_ids=[str(item) for item in user_ids],
    )
    return {
        "message": f"Deleted {deleted} users. Skipped {skipped}.",
        "deleted_count": deleted,
        "skipped_count": skipped,
    }


@router.post(
    "/users/bulk/invite", dependencies=[Depends(require_permission("rbac:assign"))]
)
def users_bulk_invite(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    user_ids = data.get("user_ids", [])
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids is required")

    try:
        normalized_ids = tuple(dict.fromkeys(UUID(str(item)) for item in user_ids))
        commands = tuple(
            staff_provisioning_service.PrepareStaffCredentialRecoveryCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative bulk staff invitation preparation",
                    idempotency_key=f"staff-recovery:{user_id}:invite",
                ),
                user_id=user_id,
            )
            for user_id in normalized_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid user id") from exc
    sent, failed = web_system_user_mutations_service.bulk_send_user_invites(
        db,
        commands=commands,
    )
    return {
        "message": f"Sent {sent} invite(s). Failed {failed}.",
        "sent_count": sent,
        "failed_count": failed,
    }


@router.get(
    "/users/{user_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def user_detail(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    detail_data = web_system_profiles_service.get_user_detail_data(db, user_id)
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/system/users/detail.html",
        {
            "request": request,
            "user": detail_data["user"],
            "roles": detail_data["roles"],
            "credential": detail_data["credential"],
            "credential_issue": detail_data["credential_issue"],
            "credential_issue_code": detail_data["credential_issue_code"],
            "credential_recovery": detail_data["credential_recovery"],
            "mfa_methods": detail_data["mfa_methods"],
            "audit_items": _system_user_audit_items(db, user_id),
            "nextcloud_talk_accounts": (
                nextcloud_talk_staff_service.staff_account_mapping_rows(
                    db, system_user_id=coerce_uuid(user_id)
                )
            ),
            "active_page": "users",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/users/{user_id}/nextcloud-talk",
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_nextcloud_talk_mapping_save(
    request: Request,
    user_id: str,
    installation_id: UUID = Form(...),
    nextcloud_username: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_system_user_principal(request)
    try:
        nextcloud_talk_staff_service.execute_set_staff_account_mapping(
            db,
            nextcloud_talk_staff_service.SetStaffAccountMappingCommand(
                context=_system_command_context(
                    request,
                    scope=nextcloud_talk_staff_service.COMMAND_SCOPE,
                    reason="Administrative Nextcloud Talk staff mapping update",
                    idempotency_key=(
                        f"nextcloud-talk-mapping:{user_id}:{installation_id}:"
                        f"{nextcloud_username.strip().casefold()}"
                    ),
                ),
                system_user_id=coerce_uuid(user_id),
                integration_installation_id=installation_id,
                nextcloud_username=nextcloud_username,
            ),
        )
    except (DomainError, ValueError, IntegrityError) as exc:
        detail = exc.message if isinstance(exc, DomainError) else str(exc)
        raise HTTPException(status_code=409, detail=detail) from exc
    return RedirectResponse(
        f"/admin/system/users/{user_id}?talk_mapping_saved=1",
        status_code=303,
    )


@router.post(
    "/users/{user_id}/nextcloud-talk/{installation_id}/disable",
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_nextcloud_talk_mapping_disable(
    request: Request,
    user_id: str,
    installation_id: UUID,
    db: Session = Depends(get_db),
):
    _require_system_user_principal(request)
    try:
        nextcloud_talk_staff_service.execute_disable_staff_account_mapping(
            db,
            nextcloud_talk_staff_service.DisableStaffAccountMappingCommand(
                context=_system_command_context(
                    request,
                    scope=nextcloud_talk_staff_service.COMMAND_SCOPE,
                    reason="Administrative Nextcloud Talk staff mapping disable",
                    idempotency_key=(
                        f"nextcloud-talk-mapping-disable:{user_id}:{installation_id}"
                    ),
                ),
                system_user_id=coerce_uuid(user_id),
                integration_installation_id=installation_id,
            ),
        )
    except (DomainError, ValueError) as exc:
        detail = exc.message if isinstance(exc, DomainError) else str(exc)
        raise HTTPException(status_code=409, detail=detail) from exc
    return RedirectResponse(
        f"/admin/system/users/{user_id}?talk_mapping_disabled=1",
        status_code=303,
    )


@router.post(
    "/users/{user_id}/nextcloud-talk/{installation_id}/test",
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_nextcloud_talk_mapping_test(
    request: Request,
    user_id: str,
    installation_id: UUID,
    db: Session = Depends(get_db),
):
    _require_system_user_principal(request)
    try:
        nextcloud_talk_staff_service.execute_test_staff_talk_connection(
            db,
            nextcloud_talk_staff_service.TestStaffTalkConnectionCommand(
                context=_system_command_context(
                    request,
                    scope=nextcloud_talk_staff_service.COMMAND_SCOPE,
                    reason="Administrative Nextcloud Talk connection test",
                    idempotency_key=(
                        f"nextcloud-talk-test:{user_id}:{installation_id}:{uuid4()}"
                    ),
                ),
                system_user_id=coerce_uuid(user_id),
                integration_installation_id=installation_id,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return RedirectResponse(
        f"/admin/system/users/{user_id}?talk_test_sent=1",
        status_code=303,
    )


@router.get(
    "/users/{user_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_edit(request: Request, user_id: str, db: Session = Depends(get_db)):
    from app.services.auth_dependencies import has_permission
    from app.services.device_login import derive_router_tier
    from app.services.radius_population import effective_perms, effective_roles
    from app.web.admin import get_current_user, get_sidebar_stats

    edit_data = web_system_profiles_service.get_user_edit_data(db, user_id)
    if not edit_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )

    # Device-login panel: only expose to admins with router:admin
    auth = getattr(request.state, "auth", None) or {}
    can_manage_device_login = bool(auth) and has_permission(auth, db, "router:admin")
    device_login_tier: str | None = None
    if can_manage_device_login:
        try:
            target_user = edit_data["user"]
            roles = effective_roles(db, target_user.id)
            perms = effective_perms(db, target_user.id)
            device_login_tier = derive_router_tier(roles, perms)
        except Exception:
            device_login_tier = None

    ctx: dict = {
        "request": request,
        "user": edit_data["user"],
        "roles": edit_data["roles"],
        "current_role_ids": edit_data["current_role_ids"],
        "managed_role_ids": edit_data["managed_role_ids"],
        "all_permissions": edit_data["all_permissions"],
        "direct_permission_ids": edit_data["direct_permission_ids"],
        "audit_items": _system_user_audit_items(db, user_id),
        "user_type_options": web_system_users_service.USER_TYPE_OPTIONS,
        "can_update_password": web_system_common_service.is_admin_request(request),
        "active_page": "users",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }
    if can_manage_device_login:
        ctx["device_login_tier"] = device_login_tier

    return templates.TemplateResponse("admin/system/users/edit.html", ctx)


@router.post(
    "/users/{user_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_edit_submit(
    request: Request,
    user_id: str,
    form_data=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        normalized_user_id = UUID(user_id)
    except ValueError:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "User not found"},
            status_code=404,
        )
    parsed = web_system_user_edit_service.parse_edit_form(form_data)
    desired_fingerprint = hashlib.sha256(
        "\x1f".join(
            (
                parsed.first_name,
                parsed.last_name,
                parsed.display_name or "",
                parsed.email,
                parsed.phone or "",
                parsed.new_password or "",
                str(parsed.require_password_change),
            )
        ).encode()
    ).hexdigest()

    try:
        command = web_system_user_edit_service.build_update_command(
            user_id=normalized_user_id,
            context=_system_command_context(
                request,
                reason="Administrative staff identity update",
                idempotency_key=(
                    f"staff-identity:{normalized_user_id}:{desired_fingerprint}"
                ),
            ),
            form=parsed,
            can_update_password=web_system_common_service.is_admin_request(request),
        )
        staff_provisioning_service.update_staff_identity(
            db,
            command,
        )
    except (DomainError, ValueError) as exc:
        edit_data = web_system_profiles_service.get_user_edit_data(db, user_id)
        if edit_data is None:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "User not found"},
                status_code=404,
            )
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return templates.TemplateResponse(
            "admin/system/users/edit.html",
            {
                "request": request,
                **edit_data,
                "audit_items": _system_user_audit_items(db, user_id),
                "user_type_options": web_system_users_service.USER_TYPE_OPTIONS,
                "can_update_password": web_system_common_service.is_admin_request(
                    request
                ),
                "active_page": "users",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "error": message,
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/assignments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_assignments_submit(
    request: Request,
    user_id: str,
    form_data=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        resolved_user_id = UUID(user_id)
        role_ids = tuple(UUID(value) for value in form_data.getlist("role_ids"))
        permission_ids = tuple(
            UUID(value) for value in form_data.getlist("direct_permission_ids")
        )
        assignment_service.replace_system_user_assignments(
            db,
            assignment_service.ReplaceSystemUserAssignmentsCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative system-user access replacement",
                    idempotency_key=(
                        f"system-user-assignments:{user_id}:"
                        f"{','.join(sorted(map(str, role_ids)))}:"
                        f"{','.join(sorted(map(str, permission_ids)))}"
                    ),
                ),
                user_id=resolved_user_id,
                role_ids=role_ids,
                direct_permission_ids=permission_ids,
            ),
        )
    except (DomainError, ValueError) as exc:
        edit_data = web_system_profiles_service.get_user_edit_data(db, user_id)
        if not edit_data:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "User not found"},
                status_code=404,
            )
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return templates.TemplateResponse(
            "admin/system/users/edit.html",
            {
                "request": request,
                **edit_data,
                "audit_items": _system_user_audit_items(db, user_id),
                "user_type_options": web_system_users_service.USER_TYPE_OPTIONS,
                "can_update_password": web_system_common_service.is_admin_request(
                    request
                ),
                "active_page": "users",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "error": message,
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_activate(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        staff_provisioning_service.set_staff_account_active(
            db,
            staff_provisioning_service.SetStaffAccountActiveCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative staff account activation",
                    idempotency_key=f"staff-account-active:{user_id}:true",
                ),
                user_id=UUID(user_id),
                is_active=True,
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return web_system_common_service.error_banner(message)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_deactivate(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        staff_provisioning_service.set_staff_account_active(
            db,
            staff_provisioning_service.SetStaffAccountActiveCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative staff account deactivation",
                    idempotency_key=f"staff-account-active:{user_id}:false",
                ),
                user_id=UUID(user_id),
                is_active=False,
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return web_system_common_service.error_banner(message)
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/disable-mfa",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_disable_mfa(request: Request, user_id: str, db: Session = Depends(get_db)):
    web_system_user_mutations_service.disable_user_mfa(
        db, user_id=user_id, actor_id=_system_actor_id(request)
    )
    return Response(status_code=204)


@router.post(
    "/users/{user_id}/reset-password",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_reset_password(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        note = web_system_user_mutations_service.send_password_reset_link_for_user(
            db,
            command=staff_provisioning_service.PrepareStaffCredentialRecoveryCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative staff password recovery preparation",
                    idempotency_key=f"staff-recovery:{user_id}:password-reset",
                ),
                user_id=UUID(user_id),
            ),
        )
        if "queued" in note.lower():
            _log_system_user_event(
                db,
                request,
                action="password_reset_sent",
                user_id=user_id,
                metadata={"delivery": note},
            )
    except Exception as exc:
        note = str(exc)
    success = "success" if "queued" in note.lower() else "error"
    trigger = {
        "showToast": {
            "type": success,
            "title": "Password reset",
            "message": note,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.get(
    "/users/{user_id}/reset-password",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_reset_password_get_fallback(user_id: str):
    """Fallback for auth-refresh GET redirect on reset-password action URLs."""
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_send_invite(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        note = web_system_user_mutations_service.send_user_invite_for_user(
            db,
            command=staff_provisioning_service.PrepareStaffCredentialRecoveryCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative staff invitation preparation",
                    idempotency_key=f"staff-recovery:{user_id}:invite",
                ),
                user_id=UUID(user_id),
            ),
        )
        if web_system_user_mutations_service.user_invite_succeeded(note):
            _log_system_user_event(
                db,
                request,
                action="invite_sent",
                user_id=user_id,
                metadata={"delivery": note},
            )
    except Exception as exc:
        note = str(exc)
    success = (
        "success"
        if web_system_user_mutations_service.user_invite_succeeded(note)
        else "error"
    )
    trigger = {
        "showToast": {
            "type": success,
            "title": "User invite",
            "message": note,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.get(
    "/users/{user_id}/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_send_invite_get_fallback(user_id: str):
    """Fallback for auth-refresh GET redirect on invite action URLs."""
    return RedirectResponse(url=f"/admin/system/users/{user_id}", status_code=303)


@router.post(
    "/users/{user_id}/device-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:admin"))],
)
def user_device_login_set(
    request: Request,
    user_id: str,
    form_data=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Enable/disable device login and set/rotate the router secret."""
    from app.services.device_login import derive_router_tier
    from app.services.radius_population import effective_perms, effective_roles
    from app.tasks.radius_population import sync_device_login

    action = form_data.get("device_login_action", "set")
    note: str
    try:
        if action == "revoke":
            web_system_user_mutations_service.revoke_device_login(
                db, user_id=user_id, commit=False
            )
            note = "Device login revoked."
        else:
            enabled = bool(form_data.get("device_login_enabled"))
            secret = form_data.get("device_login_secret") or None
            if enabled:
                roles = effective_roles(db, UUID(user_id))
                perms = effective_perms(db, UUID(user_id))
                if derive_router_tier(roles, perms) is None:
                    raise ValueError(
                        "Router device login is only available to router admins."
                    )
            web_system_user_mutations_service.set_device_login(
                db, user_id=user_id, enabled=enabled, secret=secret, commit=False
            )
            note = "Device login updated."
        _log_system_user_event(
            db,
            request,
            action=f"device_login_{action}",
            user_id=user_id,
            metadata={"action": action},
        )
        # Commit the credential change and its audit row atomically, so a
        # failure in either rolls BOTH back (no "changed but reported failed").
        db.commit()
    except ValueError as exc:
        db.rollback()
        note = str(exc)
        trigger = {
            "showToast": {
                "type": "error",
                "title": "Device login",
                "message": note,
                "duration": 8000,
            }
        }
        if request.headers.get("HX-Request"):
            return Response(
                status_code=400, headers={"HX-Trigger": json.dumps(trigger)}
            )
        return RedirectResponse(
            url=f"/admin/system/users/{user_id}/edit", status_code=303
        )
    except Exception:
        db.rollback()
        note = "Device-login update failed"
        trigger = {
            "showToast": {
                "type": "error",
                "title": "Device login",
                "message": note,
                "duration": 8000,
            }
        }
        if request.headers.get("HX-Request"):
            return Response(
                status_code=400, headers={"HX-Trigger": json.dumps(trigger)}
            )
        return RedirectResponse(
            url=f"/admin/system/users/{user_id}/edit", status_code=303
        )

    # Best-effort sync: the credential change + audit are already committed. A
    # broker/enqueue failure must NOT report failure to the operator — the
    # periodic device_login_sync sweep reconciles RADIUS regardless.
    try:
        sync_device_login.delay()
    except Exception:
        logger.warning(
            "device-login sync enqueue failed; periodic sweep will reconcile",
            exc_info=True,
        )

    trigger = {
        "showToast": {
            "type": "success",
            "title": "Device login",
            "message": note,
            "duration": 6000,
        }
    }
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Trigger": json.dumps(trigger)})
    return RedirectResponse(url=f"/admin/system/users/{user_id}/edit", status_code=303)


@router.get(
    "/users/{user_id}/device-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:admin"))],
)
def user_device_login_get_fallback(user_id: str):
    """Fallback for auth-refresh GET redirect on device-login action URLs."""
    return RedirectResponse(url=f"/admin/system/users/{user_id}/edit", status_code=303)


@router.post(
    "/users",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_create(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    role_id: str = Form(...),
    user_type: str = Form("system_user"),
    send_invite: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        outcome = staff_provisioning_service.create_local_staff_account(
            db,
            staff_provisioning_service.CreateLocalStaffAccountCommand(
                context=_system_command_context(
                    request,
                    reason="Administrative local staff account creation",
                    idempotency_key=f"local-staff-account:{email.strip().lower()}",
                ),
                email=email,
                first_name=first_name,
                last_name=last_name,
                role_id=UUID(role_id),
                send_invite=bool(send_invite),
            ),
        )
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return web_system_common_service.error_banner(message)

    note = "User created. Ask the user to reset their password."
    if outcome.invite_requested:
        note = "User created. Invitation delivery has been queued."
    return HTMLResponse(
        '<div class="rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">'
        f"{note}"
        "</div>"
    )


@router.delete(
    "/users/{user_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def user_delete(request: Request, user_id: str, db: Session = Depends(get_db)):
    system_user = web_system_user_edit_service.get_subscriber_or_none(db, user_id)
    if not system_user:
        raise HTTPException(status_code=404, detail="User not found")
    if system_user.is_active:
        return web_system_common_service.blocked_delete_response(
            request, [], detail="Deactivate user before deleting."
        )
    deleted_metadata = {
        "email": system_user.email,
        "display_name": system_user.display_name
        or f"{system_user.first_name or ''} {system_user.last_name or ''}".strip(),
    }
    try:
        web_system_user_mutations_service.delete_user_records(db, user_id=user_id)
        _log_system_user_event(
            db,
            request,
            action="delete",
            user_id=user_id,
            metadata=deleted_metadata,
        )
    except IntegrityError:
        db.rollback()
        return web_system_common_service.blocked_delete_response(
            request, [], detail="User cannot be deleted due to linked records."
        )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/admin/system/users"})
    return RedirectResponse(url="/admin/system/users", status_code=303)


@router.get(
    "/roles",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def roles_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List roles and permissions."""
    page_data = web_system_roles_service.get_roles_page_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/roles.html",
        {
            "request": request,
            **page_data,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/roles/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def role_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_system_form_views_service.get_role_new_form_context(db)
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/roles",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def role_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    permission_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    payload = web_system_role_forms_service.build_role_create_payload(
        name=name,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        web_system_role_forms_service.create_role_with_permissions(
            db,
            payload=payload,
            permission_ids=permission_ids,
            context=_system_command_context(
                request,
                scope=rbac_catalog.ROLE_WRITE_SCOPE,
                reason="Admin role catalog creation",
                idempotency_key=f"rbac-role-create:{payload.name.lower()}",
            ),
        )
    except (DomainError, ValueError) as exc:
        error_message = exc.message if isinstance(exc, DomainError) else str(exc)
        error_state = web_system_role_forms_service.build_role_error_state(
            db,
            role=payload.model_dump(),
            permission_ids=permission_ids,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                **error_state,
                "action_url": "/admin/system/roles",
                "form_title": "New Role",
                "submit_label": "Create Role",
                "error": error_message,
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.get(
    "/roles/{role_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def role_edit(request: Request, role_id: str, db: Session = Depends(get_db)):
    try:
        form_data = web_system_role_forms_service.get_role_edit_data(db, role_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Role not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/roles_form.html",
        {
            "request": request,
            "role": form_data["role"],
            "permissions": form_data["permissions"],
            "selected_permission_ids": form_data["selected_permission_ids"],
            "action_url": f"/admin/system/roles/{role_id}",
            "form_title": "Edit Role",
            "submit_label": "Save Changes",
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/roles/{role_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def role_update(
    request: Request,
    role_id: str,
    name: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    permission_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    payload = web_system_role_forms_service.build_role_update_payload(
        name=name,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        web_system_role_forms_service.update_role_with_permissions(
            db,
            role_id=role_id,
            payload=payload,
            permission_ids=permission_ids,
            context=_system_command_context(
                request,
                scope=rbac_catalog.ROLE_WRITE_SCOPE,
                reason="Admin role catalog update",
                idempotency_key=f"rbac-role-update:{role_id}",
            ),
        )
    except (DomainError, ValueError) as exc:
        error_message = exc.message if isinstance(exc, DomainError) else str(exc)
        error_state = web_system_role_forms_service.build_role_error_state(
            db=db,
            role={"id": role_id, **payload.model_dump()},
            permission_ids=permission_ids,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/system/roles_form.html",
            {
                "request": request,
                **error_state,
                "action_url": f"/admin/system/roles/{role_id}",
                "form_title": "Edit Role",
                "submit_label": "Save Changes",
                "error": error_message,
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.post(
    "/roles/{role_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:roles:delete"))],
)
def role_delete(request: Request, role_id: str, db: Session = Depends(get_db)):
    try:
        rbac_catalog.deactivate_role(
            db,
            rbac_catalog.DeactivateRoleCommand(
                context=_system_command_context(
                    request,
                    scope=rbac_catalog.ROLE_DELETE_SCOPE,
                    reason="Admin role catalog deactivation",
                    idempotency_key=f"rbac-role-deactivate:{role_id}",
                ),
                role_id=UUID(role_id),
            ),
        )
    except (DomainError, ValueError) as exc:
        detail = exc.message if isinstance(exc, DomainError) else "Invalid role ID"
        raise HTTPException(status_code=409, detail=detail) from exc
    return RedirectResponse(url="/admin/system/roles", status_code=303)


@router.get(
    "/permissions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:read"))],
)
def permissions_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    page_data = web_system_roles_service.get_permissions_page_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/permissions.html",
        {
            "request": request,
            **page_data,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/permissions/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def permission_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_system_form_views_service.get_permission_new_form_context()
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/permissions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def permission_create(
    request: Request,
    key: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    payload = web_system_permission_forms_service.build_permission_create_payload(
        key=key,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        rbac_catalog.create_permission(
            db,
            rbac_catalog.CreatePermissionCommand(
                context=_system_command_context(
                    request,
                    scope=rbac_catalog.PERMISSION_WRITE_SCOPE,
                    reason="Admin permission catalog creation",
                    idempotency_key=(f"rbac-permission-create:{payload.key.lower()}"),
                ),
                key=payload.key,
                description=payload.description,
                is_active=payload.is_active,
                is_ui_assignable=payload.is_ui_assignable,
            ),
        )
    except DomainError as exc:
        error_state = web_system_permission_forms_service.build_permission_error_state(
            permission=payload.model_dump(),
            action_url="/admin/system/permissions",
            form_title="New Permission",
            submit_label="Create Permission",
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                **error_state,
                "error": exc.message,
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.get(
    "/permissions/{permission_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def permission_edit(
    request: Request, permission_id: str, db: Session = Depends(get_db)
):
    form_context = web_system_form_views_service.get_permission_edit_form_context(
        db,
        permission_id,
    )
    if form_context is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Permission not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/permissions_form.html",
        {
            "request": request,
            **form_context,
            "active_page": "roles",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/permissions/{permission_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def permission_update(
    request: Request,
    permission_id: str,
    key: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    payload = web_system_permission_forms_service.build_permission_update_payload(
        key=key,
        description=description,
        is_active=web_system_common_service.form_bool(is_active),
    )
    try:
        rbac_catalog.update_permission(
            db,
            rbac_catalog.UpdatePermissionCommand(
                context=_system_command_context(
                    request,
                    scope=rbac_catalog.PERMISSION_WRITE_SCOPE,
                    reason="Admin permission catalog update",
                    idempotency_key=f"rbac-permission-update:{permission_id}",
                ),
                permission_id=UUID(permission_id),
                key=payload.key,
                description=payload.description,
                update_description="description" in payload.model_fields_set,
                is_active=payload.is_active,
                is_ui_assignable=payload.is_ui_assignable,
            ),
        )
    except (DomainError, ValueError) as exc:
        error_message = exc.message if isinstance(exc, DomainError) else str(exc)
        error_state = web_system_permission_forms_service.build_permission_error_state(
            permission={"id": permission_id, **payload.model_dump()},
            action_url=f"/admin/system/permissions/{permission_id}",
            form_title="Edit Permission",
            submit_label="Save Changes",
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/system/permissions_form.html",
            {
                "request": request,
                **error_state,
                "error": error_message,
                "active_page": "roles",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.post(
    "/permissions/{permission_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("rbac:permissions:delete"))],
)
def permission_delete(
    request: Request, permission_id: str, db: Session = Depends(get_db)
):
    try:
        rbac_catalog.deactivate_permission(
            db,
            rbac_catalog.DeactivatePermissionCommand(
                context=_system_command_context(
                    request,
                    scope=rbac_catalog.PERMISSION_DELETE_SCOPE,
                    reason="Admin permission catalog deactivation",
                    idempotency_key=f"rbac-permission-deactivate:{permission_id}",
                ),
                permission_id=UUID(permission_id),
            ),
        )
    except (DomainError, ValueError) as exc:
        detail = (
            exc.message if isinstance(exc, DomainError) else "Invalid permission ID"
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    return RedirectResponse(url="/admin/system/permissions", status_code=303)


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_list(request: Request, revoked: str = "", db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    person_id = current_user.get("person_id") if current_user else None
    api_keys = web_system_api_keys_service.list_api_keys_for_subscriber(db, person_id)

    # ``new_key`` is intentionally never read from the query string — the raw
    # secret is shown once on the create/rotate POST response, not via a URL
    # param. A revoke has no secret, so it uses a redirect query flag banner.
    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": current_user,
        "sidebar_stats": get_sidebar_stats(db),
        "api_keys": api_keys,
        "new_key": None,
        "revoked": bool(revoked),
        "now": datetime.now(UTC),
    }
    return templates.TemplateResponse("admin/system/api_keys.html", context)


@router.get("/api-keys/new", response_class=HTMLResponse)
def api_key_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    form_context = web_system_api_key_forms_service.get_api_key_new_form_context()
    context = {
        "request": request,
        "active_page": "api-keys",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **form_context,
    }
    return templates.TemplateResponse("admin/system/api_key_form.html", context)


@router.post(
    "/api-keys",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def api_key_create(
    request: Request,
    label: str = Form(...),
    expires_in: str = Form(None),
    scopes: str = Form(None),
    system_owned: str = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/admin/system/api-keys", status_code=303)

    person_id = current_user.get("person_id")
    # A "system-owned" key has no subscriber owner (service/integration key).
    # Otherwise the key is owned by the calling admin's subscriber principal, so
    # that still requires a person_id.
    owner_id = None if system_owned else person_id
    if not system_owned and not person_id:
        return RedirectResponse(url="/admin/system/api-keys", status_code=303)

    try:
        raw_key = web_system_api_key_forms_service.create_api_key(
            db,
            subscriber_id=owner_id,
            label=label,
            expires_in=expires_in,
            scopes=web_system_api_key_forms_service.parse_scopes(scopes),
        )

        # Show the raw secret exactly once, in the POST response body — NOT in a
        # redirect URL (which leaks into access logs / history / Referer and
        # re-shows on reload).
        api_keys = web_system_api_keys_service.list_api_keys_for_subscriber(
            db, person_id
        )
        return templates.TemplateResponse(
            "admin/system/api_keys.html",
            {
                "request": request,
                "active_page": "api-keys",
                "active_menu": "system",
                "current_user": current_user,
                "sidebar_stats": get_sidebar_stats(db),
                "api_keys": api_keys,
                "new_key": raw_key,
                "now": datetime.now(UTC),
            },
        )
    except Exception as e:
        db.rollback()
        context = {
            "request": request,
            "active_page": "api-keys",
            "active_menu": "system",
            "current_user": current_user,
            "sidebar_stats": get_sidebar_stats(db),
            "error": str(e),
        }
        return templates.TemplateResponse("admin/system/api_key_form.html", context)


@router.post(
    "/api-keys/{key_id}/revoke",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def api_key_revoke(request: Request, key_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    person_id = current_user.get("person_id") if current_user else None
    # Scope the revoke to the caller's own keys so a staffer can't revoke another
    # principal's key by guessing/enumerating ids.
    web_system_api_key_mutations_service.revoke_api_key(
        db, key_id=key_id, subscriber_id=person_id
    )
    # No secret to surface on a revoke, so use PRG + a query flag the list page
    # renders as a "revoked" banner (create/rotate keep the secret out of URLs).
    return RedirectResponse(url="/admin/system/api-keys?revoked=1", status_code=303)


@router.post(
    "/api-keys/{key_id}/rotate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def api_key_rotate(request: Request, key_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    current_user = get_current_user(request)
    person_id = current_user.get("person_id") if current_user else None
    raw_key = web_system_api_key_mutations_service.rotate_api_key(
        db, key_id=key_id, subscriber_id=person_id
    )
    api_keys = web_system_api_keys_service.list_api_keys_for_subscriber(db, person_id)
    if not raw_key:
        # Missing / out-of-scope / already-revoked key: no secret to show.
        return RedirectResponse(url="/admin/system/api-keys", status_code=303)
    # Show the rotated secret exactly once in the POST response body — same
    # one-time-reveal pattern as create, never via a redirect URL.
    return templates.TemplateResponse(
        "admin/system/api_keys.html",
        {
            "request": request,
            "active_page": "api-keys",
            "active_menu": "system",
            "current_user": current_user,
            "sidebar_stats": get_sidebar_stats(db),
            "api_keys": api_keys,
            "new_key": raw_key,
            "rotated": True,
            "now": datetime.now(UTC),
        },
    )


@router.get(
    "/audit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("audit:read"))],
)
def audit_log(
    request: Request,
    actor_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View audit log."""
    try:
        list_query = web_system_audit_service.build_audit_list_query(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        )
    except ValueError:
        # Out-of-contract sort/page params (e.g. hand-edited URL) fall back to
        # the contract defaults rather than erroring the page.
        list_query = web_system_audit_service.build_audit_list_query(page=page)

    page_data = web_system_audit_service.get_audit_page_data(db, list_query)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/system/_audit_table.html",
            {
                "request": request,
                **page_data,
            },
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/audit.html",
        {
            "request": request,
            **page_data,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "active_page": "audit",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/audit-log", dependencies=[Depends(require_permission("audit:read"))])
def audit_log_legacy():
    """Legacy route redirect for audit log."""
    return RedirectResponse(url="/admin/system/audit", status_code=307)


@router.get(
    "/scheduler",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def scheduler_overview(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View scheduled tasks."""
    page_data = web_system_scheduler_service.get_scheduler_overview_data(
        db,
        page=page,
        per_page=per_page,
    )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/system/scheduler.html",
        {
            "request": request,
            **page_data,
            "active_page": "scheduler",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/scheduler/{task_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def scheduler_task_detail(
    request: Request, task_id: str, db: Session = Depends(get_db)
):
    """View scheduled task details."""
    from app.web.admin import get_current_user, get_sidebar_stats

    detail_data = web_system_scheduler_service.get_scheduler_task_detail_data(
        db, task_id
    )
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Scheduled task not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/system/scheduler_detail.html",
        {
            "request": request,
            "task": detail_data["task"],
            "task_is_permanent": detail_data["task_is_permanent"],
            "next_run": detail_data["next_run"],
            "active_timezone": detail_data["active_timezone"],
            "runs": detail_data["runs"],
            "active_page": "scheduler",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/scheduler/{task_id}/schedule",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def scheduler_task_update_schedule(
    request: Request,
    task_id: str,
    schedule_type: str = Form(...),
    interval_seconds: str = Form(default=""),
    cron_expr: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Update a scheduled task's cadence (interval seconds or cron expression)."""
    from app.models.scheduler import ScheduleType
    from app.schemas.scheduler import ScheduledTaskUpdate
    from app.services import scheduler_config

    base = f"/admin/system/scheduler/{task_id}"
    if schedule_type == "crontab":
        expr = cron_expr.strip()
        if not scheduler_config.is_valid_cron(expr):
            return RedirectResponse(url=f"{base}?error=invalid_cron", status_code=303)
        update = ScheduledTaskUpdate(schedule_type=ScheduleType.crontab, cron_expr=expr)
    else:
        try:
            seconds = int(interval_seconds)
            if seconds < 1:
                raise ValueError
        except (TypeError, ValueError):
            return RedirectResponse(
                url=f"{base}?error=invalid_interval", status_code=303
            )
        update = ScheduledTaskUpdate(
            schedule_type=ScheduleType.interval,
            interval_seconds=seconds,
            cron_expr=None,
        )
    scheduler_service.scheduled_tasks.update(db, task_id, update)
    return RedirectResponse(url=f"{base}?saved=1", status_code=303)


@router.post(
    "/scheduler/{task_id}/enable",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def scheduler_task_enable(
    request: Request, task_id: str, db: Session = Depends(get_db)
):
    """Enable a scheduled task."""
    from app.schemas.scheduler import ScheduledTaskUpdate

    scheduler_service.scheduled_tasks.update(
        db, task_id, ScheduledTaskUpdate(enabled=True)
    )
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post(
    "/scheduler/{task_id}/disable",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def scheduler_task_disable(
    request: Request, task_id: str, db: Session = Depends(get_db)
):
    """Disable a scheduled task."""
    from app.schemas.scheduler import ScheduledTaskUpdate

    scheduler_service.scheduled_tasks.update(
        db, task_id, ScheduledTaskUpdate(enabled=False)
    )
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post(
    "/scheduler/{task_id}/run",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def scheduler_task_run(request: Request, task_id: str, db: Session = Depends(get_db)):
    """Manually trigger a scheduled task."""
    scheduler_service.enqueue_by_id(db, task_id)
    return RedirectResponse(url=f"/admin/system/scheduler/{task_id}", status_code=303)


@router.post(
    "/scheduler/{task_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def scheduler_task_delete(
    request: Request, task_id: str, db: Session = Depends(get_db)
):
    """Delete a scheduled task."""
    scheduler_service.scheduled_tasks.delete(db, task_id)
    return RedirectResponse(url="/admin/system/scheduler", status_code=303)


@router.get(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def settings_overview(
    request: Request,
    domain: str | None = None,
    db: Session = Depends(get_db),
):
    """System settings management."""
    if domain == web_system_settings_views_service.BRANDING_DOMAIN:
        return RedirectResponse(url="/admin/system/branding", status_code=303)
    if domain == "notification":
        return RedirectResponse(url="/admin/system/email", status_code=303)

    settings_context = web_system_settings_views_service.build_settings_context(
        db, domain
    )
    context = web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
    )
    return templates.TemplateResponse(
        "admin/system/settings.html",
        context,
    )


@router.post(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_update(
    request: Request,
    domain: str | None = Form(None),
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Update system settings for a domain."""
    domain_value = domain or form.get("domain")
    settings_context, errors = (
        web_system_settings_forms_service.process_settings_update(
            db=db,
            domain_value=domain_value,
            form=form,
        )
    )
    if domain_value == "notification":
        if not errors:
            return RedirectResponse(url="/admin/system/email?saved=1", status_code=303)
        return templates.TemplateResponse(
            "admin/system/email.html",
            _smtp_page_context(request, db, {"errors": errors}),
            status_code=400,
        )
    context = web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
        extra={"errors": errors, "saved": not errors},
    )
    return templates.TemplateResponse(
        "admin/system/settings.html",
        context,
    )


@router.post(
    "/settings/branding",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_branding_update(
    request: Request,
    main_logo_url: str | None = Form(None),
    dark_logo_url: str | None = Form(None),
    favicon_url: str | None = Form(None),
    remove_main_logo: str | None = Form(None),
    remove_dark_logo: str | None = Form(None),
    remove_favicon: str | None = Form(None),
    main_logo_file: UploadFile | None = File(None),
    dark_logo_file: UploadFile | None = File(None),
    favicon_file: UploadFile | None = File(None),
    brand_primary_color: str | None = Form(None),
    brand_secondary_color: str | None = Form(None),
    brand_semantic_positive_color: str | None = Form(None),
    brand_semantic_info_color: str | None = Form(None),
    brand_semantic_warning_color: str | None = Form(None),
    brand_semantic_negative_color: str | None = Form(None),
    brand_semantic_neutral_color: str | None = Form(None),
    login_hero_customer_url: str | None = Form(None),
    login_hero_reseller_url: str | None = Form(None),
    login_hero_admin_url: str | None = Form(None),
    remove_login_hero_customer: str | None = Form(None),
    remove_login_hero_reseller: str | None = Form(None),
    remove_login_hero_admin: str | None = Form(None),
    login_hero_customer_file: UploadFile | None = File(None),
    login_hero_reseller_file: UploadFile | None = File(None),
    login_hero_admin_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Update sidebar branding assets via URL or file upload."""
    domain = SettingDomain.comms
    from app.web.admin import get_current_user as get_admin_current_user

    def _is_local_branding_path(value: str) -> bool:
        return value.startswith("/static/branding/")

    def _validate_url(url_value: str) -> str:
        candidate = url_value.strip()
        if not candidate:
            return ""
        if (
            candidate.startswith("http://")
            or candidate.startswith("https://")
            or candidate.startswith("/static/")
            or candidate.startswith("/branding/assets/")
        ):
            return candidate
        raise ValueError(
            "Branding URLs must start with http://, https://, /static/, or /branding/assets/."
        )

    def _resolve_next_value(
        *,
        key: str,
        incoming_url: str | None,
        remove_requested: bool,
        incoming_file: UploadFile | None,
        subdir: str,
        prefix: str,
    ) -> tuple[str, str]:
        current_raw = settings_spec.resolve_value(db, domain, key)
        current_value = str(current_raw).strip() if current_raw else ""
        next_value = current_value

        if remove_requested:
            return current_value, ""

        if incoming_file and incoming_file.filename:
            file_bytes = incoming_file.file.read()
            if not file_bytes:
                raise ValueError("Uploaded file is empty.")
            file_record = branding_storage_service.upload_branding_asset(
                db=db,
                setting_key=key,
                file_data=file_bytes,
                content_type=incoming_file.content_type,
                filename=incoming_file.filename,
                uploaded_by=_resolve_uploaded_by_subscriber_id(),
            )
            next_value = branding_storage_service.branding_url_for_file(file_record.id)
        else:
            validated_url = _validate_url(incoming_url or "")
            if validated_url:
                next_value = validated_url

        return current_value, next_value

    def _persist_setting(key: str, value: str) -> None:
        payload = DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=value,
            value_json=None,
            is_secret=False,
            is_active=True,
        )
        settings_spec.DOMAIN_SETTINGS_SERVICE[domain].upsert_by_key(db, key, payload)

    def _resolve_uploaded_by_subscriber_id() -> str | None:
        current_user = get_admin_current_user(request) or {}
        candidate = str(current_user.get("subscriber_id") or "").strip()
        if not candidate:
            return None
        return candidate if db.get(Subscriber, candidate) else None

    def _branding_context(extra: dict | None = None) -> dict:
        settings_context = web_system_settings_views_service.build_settings_context(
            db, web_system_settings_views_service.BRANDING_DOMAIN
        )
        return _config_context(
            request,
            db,
            {
                "active_page": "system-branding",
                **settings_context,
                **(extra or {}),
            },
        )

    try:
        assets = [
            (
                web_system_settings_views_service.SIDEBAR_LOGO_SETTING_KEY,
                main_logo_url,
                web_system_common_service.form_bool(remove_main_logo),
                main_logo_file,
                "sidebar",
                "main_logo",
            ),
            (
                web_system_settings_views_service.SIDEBAR_LOGO_DARK_SETTING_KEY,
                dark_logo_url,
                web_system_common_service.form_bool(remove_dark_logo),
                dark_logo_file,
                "sidebar",
                "dark_logo",
            ),
            (
                web_system_settings_views_service.FAVICON_SETTING_KEY,
                favicon_url,
                web_system_common_service.form_bool(remove_favicon),
                favicon_file,
                "favicon",
                "favicon",
            ),
            (
                "login_hero_customer_url",
                login_hero_customer_url,
                web_system_common_service.form_bool(remove_login_hero_customer),
                login_hero_customer_file,
                "login",
                "login_hero_customer",
            ),
            (
                "login_hero_reseller_url",
                login_hero_reseller_url,
                web_system_common_service.form_bool(remove_login_hero_reseller),
                login_hero_reseller_file,
                "login",
                "login_hero_reseller",
            ),
            (
                "login_hero_admin_url",
                login_hero_admin_url,
                web_system_common_service.form_bool(remove_login_hero_admin),
                login_hero_admin_file,
                "login",
                "login_hero_admin",
            ),
        ]

        updates: list[tuple[str, str, str]] = []
        for key, url_value, remove_flag, file_value, subdir, prefix in assets:
            current_value, next_value = _resolve_next_value(
                key=key,
                incoming_url=url_value,
                remove_requested=remove_flag,
                incoming_file=file_value,
                subdir=subdir,
                prefix=prefix,
            )
            updates.append((key, current_value, next_value))

        for key, _current, next_value in updates:
            _persist_setting(key, next_value)

        for _key, current_value, next_value in updates:
            if not current_value or current_value == next_value:
                continue
            if branding_storage_service.is_managed_branding_url(current_value):
                branding_storage_service.delete_managed_branding_url(db, current_value)
                continue
            if _is_local_branding_path(current_value):
                upload = file_upload_service.get_branding_upload()
                upload.delete_by_url(current_value, "/static/branding/")

        color_candidate = (brand_primary_color or "").strip()
        if color_candidate:
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", color_candidate):
                raise ValueError(
                    f"Brand colour must be a 6-digit hex value, e.g. {DEFAULT_HEX}."
                )
            normalised = color_candidate.lstrip("#").lower()
            _persist_setting("brand_primary_color", f"#{normalised}")

        secondary_candidate = (brand_secondary_color or "").strip()
        if secondary_candidate:
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", secondary_candidate):
                raise ValueError(
                    "Brand colour must be a 6-digit hex value, "
                    f"e.g. {DEFAULT_SECONDARY_HEX}."
                )
            normalised_secondary = secondary_candidate.lstrip("#").lower()
            _persist_setting("brand_secondary_color", f"#{normalised_secondary}")

        semantic_candidates = {
            "positive": brand_semantic_positive_color,
            "info": brand_semantic_info_color,
            "warning": brand_semantic_warning_color,
            "negative": brand_semantic_negative_color,
            "neutral": brand_semantic_neutral_color,
        }
        for tone, raw_color in semantic_candidates.items():
            candidate = (raw_color or "").strip()
            if not candidate:
                continue
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", candidate):
                raise ValueError(
                    f"{tone.title()} semantic colour must be a 6-digit hex value."
                )
            normalised_semantic = f"#{candidate.lstrip('#').lower()}"
            if not is_accessible_semantic_color(normalised_semantic):
                raise ValueError(
                    f"{tone.title()} semantic colour must meet WCAG AA contrast "
                    "in light and dark themes."
                )
            _persist_setting(
                f"brand_semantic_{tone}_color",
                normalised_semantic,
            )

        from app.services.brand_profiles import (
            sync_platform_brand_from_legacy_settings_committed,
        )

        sync_platform_brand_from_legacy_settings_committed(
            db,
            overwrite_fields={
                "logo_url",
                "dark_logo_url",
                "favicon_url",
                "primary_color",
                "secondary_color",
                "semantic_colors",
            },
        )

        return RedirectResponse(url="/admin/system/branding", status_code=303)
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/system/branding.html",
            _branding_context({"errors": [str(exc)]}),
            status_code=400,
        )


def _smtp_page_context(
    request: Request,
    db: Session,
    extra: dict | None = None,
) -> dict:
    settings_context = web_system_settings_views_service.build_settings_context(
        db, "notification"
    )
    selected_sender_key = (
        str(request.query_params.get("edit_sender") or "").strip().lower()
    )
    smtp_sender_form = {
        "sender_key": "",
        "host": "",
        "port": 587,
        "username": "",
        "from_email": "",
        "from_name": "DotMac SM",
        "use_tls": True,
        "use_ssl": False,
        "is_active": True,
        "is_default": False,
        "is_editing": False,
    }
    if selected_sender_key:
        selected_sender = next(
            (
                sender
                for sender in settings_context.get("smtp_sender_profiles", [])
                if str(sender.get("sender_key", "")).strip().lower()
                == selected_sender_key
            ),
            None,
        )
        if selected_sender:
            smtp_sender_form.update(
                {
                    "sender_key": str(selected_sender.get("sender_key", "")),
                    "host": str(selected_sender.get("host", "")),
                    "port": int(selected_sender.get("port") or 587),
                    "username": str(selected_sender.get("username", "")),
                    "from_email": str(selected_sender.get("from_email", "")),
                    "from_name": str(
                        selected_sender.get("from_name", "") or "DotMac SM"
                    ),
                    "use_tls": bool(selected_sender.get("use_tls", True)),
                    "use_ssl": bool(selected_sender.get("use_ssl", False)),
                    "is_active": bool(selected_sender.get("is_active", True)),
                    "is_default": (
                        str(selected_sender.get("sender_key", "")).strip().lower()
                        == str(
                            settings_context.get("smtp_sender_default_key", "default")
                        )
                        .strip()
                        .lower()
                    ),
                    "is_editing": True,
                }
            )
    return _config_context(
        request,
        db,
        {
            "active_page": "system-email",
            **settings_context,
            "smtp_sender_form": smtp_sender_form,
            **(extra or {}),
        },
    )


@router.post(
    "/settings/smtp-senders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_upsert(
    request: Request,
    sender_key: str = Form(...),
    host: str = Form(...),
    port: int = Form(587),
    username: str | None = Form(None),
    password: str | None = Form(None),
    from_email: str = Form(...),
    from_name: str | None = Form(None),
    use_tls: str | None = Form(None),
    use_ssl: str | None = Form(None),
    is_active: str | None = Form(None),
    is_default: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        normalized_key = email_service.upsert_smtp_sender(
            db,
            sender_key=sender_key,
            host=host,
            port=port,
            username=username,
            password=password,
            from_email=from_email,
            from_name=from_name,
            use_tls=web_system_common_service.form_bool(use_tls),
            use_ssl=web_system_common_service.form_bool(use_ssl),
            is_active=web_system_common_service.form_bool(is_active),
        )
        if web_system_common_service.form_bool(is_default):
            email_service.set_default_smtp_sender_key(db, normalized_key)
        return RedirectResponse(
            url="/admin/system/email#smtp-senders",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/system/email.html",
            _smtp_page_context(request, db, {"errors": [str(exc)]}),
            status_code=400,
        )


@router.post(
    "/settings/smtp-senders/default",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_set_default(
    sender_key: str = Form(...),
    db: Session = Depends(get_db),
):
    email_service.set_default_smtp_sender_key(db, sender_key)
    return RedirectResponse(url="/admin/system/email#smtp-senders", status_code=303)


@router.post(
    "/settings/smtp-senders/{sender_key}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_delete(sender_key: str, db: Session = Depends(get_db)):
    normalized = sender_key.strip().lower()
    email_service.deactivate_smtp_sender(db, normalized)
    remaining = email_service.list_smtp_senders(db)
    current_default = email_service.get_default_smtp_sender_key(db)
    if current_default == normalized:
        if remaining:
            email_service.set_default_smtp_sender_key(
                db, str(remaining[0].get("sender_key", "default"))
            )
        else:
            email_service.set_default_smtp_sender_key(db, "default")
    return RedirectResponse(url="/admin/system/email#smtp-senders", status_code=303)


@router.post(
    "/settings/smtp-senders/{sender_key}/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_test(
    request: Request,
    sender_key: str,
    db: Session = Depends(get_db),
):
    normalized = sender_key.strip().lower()
    config = email_service.get_smtp_config(db, sender_key=normalized)
    ok, error = email_service.test_smtp_connection(config, db=db)
    message = "SMTP connection successful." if ok else (error or "SMTP test failed.")
    return templates.TemplateResponse(
        "admin/system/email.html",
        _smtp_page_context(
            request,
            db,
            extra={
                "smtp_test_result": {
                    "sender_key": normalized,
                    "ok": ok,
                    "message": message,
                }
            },
        ),
    )


@router.post(
    "/settings/smtp-senders/activities",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_smtp_sender_activities(
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    for activity_key, _ in email_service.SMTP_ACTIVITY_CHOICES:
        field_name = f"activity_{activity_key}"
        sender = form.get(field_name)
        email_service.upsert_smtp_activity_mapping(db, activity_key, sender)
    return RedirectResponse(url="/admin/system/email#smtp-senders", status_code=303)


@router.post(
    "/settings/bank-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_bank_account_create(
    request: Request,
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    try:
        payload = web_system_billing_forms_service.build_bank_account_create_payload(
            form
        )
        billing_service.bank_accounts.create(db, payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        context = web_system_billing_forms_service.build_bank_account_error_context(
            request,
            db,
            error=error,
            message="Unable to create bank account.",
        )
        return templates.TemplateResponse(
            "admin/system/settings.html",
            context,
            status_code=400,
        )


@router.post(
    "/settings/bank-accounts/{bank_account_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_bank_account_update(
    request: Request,
    bank_account_id: UUID,
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    try:
        payload = web_system_billing_forms_service.build_bank_account_update_payload(
            form
        )
        billing_service.bank_accounts.update(db, str(bank_account_id), payload)
        return RedirectResponse(
            url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
        )
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)
        context = web_system_billing_forms_service.build_bank_account_error_context(
            request,
            db,
            error=error,
            message="Unable to update bank account.",
        )
        return templates.TemplateResponse(
            "admin/system/settings.html",
            context,
            status_code=400,
        )


@router.post(
    "/settings/bank-accounts/{bank_account_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def settings_bank_account_deactivate(
    bank_account_id: UUID, db: Session = Depends(get_db)
):
    billing_service.bank_accounts.delete(db, str(bank_account_id))
    return RedirectResponse(
        url="/admin/system/settings?domain=billing#bank-accounts", status_code=303
    )


@router.get(
    "/tools/geocode",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def geocode_tool_page(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_geocode_tool_service.build_page_state(db)
    active_job_id = request.query_params.get("job_id")
    active_job = (
        web_system_geocode_tool_service.get_job(db, active_job_id)
        if active_job_id
        else None
    )
    return templates.TemplateResponse(
        "admin/system/geocode_tool.html",
        {
            "request": request,
            "active_page": "system-geocode-tool",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_job_id": active_job_id,
            "active_job": active_job,
            **state,
        },
    )


@router.post(
    "/tools/geocode/start",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def geocode_tool_start(
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    filters = web_system_geocode_tool_service.parse_filters(form)

    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = current_user.get("subscriber_id")
    job = web_system_geocode_tool_service.create_job(
        db,
        filters=filters,
        actor_id=str(actor_id) if actor_id else None,
    )
    from app.services.queue_adapter import enqueue_task

    enqueue_task(
        run_batch_geocode_job,
        kwargs={"job_id": job["job_id"]},
        correlation_id=f"batch_geocode:{job['job_id']}",
        source="admin_system_geocode",
        request_id=getattr(request.state, "request_id", None),
        actor_id=str(actor_id) if actor_id else None,
    )
    return RedirectResponse(
        url=f"/admin/system/tools/geocode?job_id={job['job_id']}",
        status_code=303,
    )


@router.get(
    "/tools/geocode/jobs/{job_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def geocode_tool_job_status(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/system/_geocode_job_status.html",
        {
            "request": request,
            "job_id": job_id,
            "job": web_system_geocode_tool_service.get_job(db, job_id),
        },
    )


@router.get(
    "/tools/restore",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def restore_tool_page(
    request: Request,
    q: str | None = None,
    selected_id: str | None = None,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_system_restore_tool_service.build_page_state(
        db,
        query=q,
        selected_id=selected_id,
    )
    return templates.TemplateResponse(
        "admin/system/restore_tool.html",
        {
            "request": request,
            "active_page": "system-restore-tool",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **state,
        },
    )


@router.post(
    "/tools/restore/{subscriber_id}",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def restore_tool_restore(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = (
        str(current_user.get("subscriber_id"))
        if current_user.get("subscriber_id")
        else None
    )

    result = web_system_restore_tool_service.restore_subscriber(
        db,
        subscriber_id=str(subscriber_id),
        actor_id=actor_id,
    )
    log_audit_event(
        db=db,
        request=request,
        action="restore",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=actor_id,
        metadata={"restored_counts": result.get("touched", {})},
    )
    return RedirectResponse(
        url=f"/admin/system/tools/restore?q={quote_plus(str(subscriber_id))}",
        status_code=303,
    )


@router.post(
    "/tools/restore/settings",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def restore_tool_settings(
    request: Request,
    retention_days: int = Form(90),
    db: Session = Depends(get_db),
):
    value = web_system_restore_tool_service.set_retention_days(
        db,
        days=retention_days,
    )
    return RedirectResponse(
        url=f"/admin/system/tools/restore?q={quote_plus(request.query_params.get('q', ''))}&selected_id={quote_plus(request.query_params.get('selected_id', ''))}&retention={value}",
        status_code=303,
    )


@router.get(
    "/tools/db-inspector",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:db_admin"))],
)
def db_inspector_page(
    request: Request,
    table: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    principal_id = _dbi_principal_id(request)
    confirmed = web_system_db_inspector_service.validate_confirmation_token(
        principal_id=principal_id,
        token=request.cookies.get(DB_INSPECTOR_COOKIE),
    )
    overview = (
        web_system_db_inspector_service.build_overview(db, selected_table=table)
        if confirmed
        else {
            "dialect": db.bind.dialect.name if db.bind is not None else "unknown",
            "table_stats": [],
            "index_usage": [],
            "slow_queries": [],
            "slow_query_note": None,
            "table_schema": [],
            "selected_table": (table or "").strip(),
        }
    )
    return templates.TemplateResponse(
        "admin/system/db_inspector.html",
        {
            "request": request,
            "active_page": "system-db-inspector",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "confirmed": confirmed,
            "query_result": None,
            "query_text": "",
            "row_limit": web_system_db_inspector_service.DEFAULT_ROWS,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
            **overview,
        },
    )


@router.post(
    "/tools/db-inspector/confirm",
    dependencies=[Depends(require_permission("system:db_admin"))],
)
def db_inspector_confirm(
    request: Request,
    db: Session = Depends(get_db),
):
    form = parse_form_data_sync(request)
    password = str(form.get("password") or "")
    table = str(form.get("table") or "").strip()
    try:
        token = web_system_db_inspector_service.confirm_access(
            db,
            principal_id=_dbi_principal_id(request),
            password=password,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/admin/system/tools/db-inspector?table={quote_plus(table)}&error={quote_plus(str(exc.detail))}",
            status_code=303,
        )

    response = RedirectResponse(
        url=f"/admin/system/tools/db-inspector?table={quote_plus(table)}&notice={quote_plus('Password confirmed. DB inspector unlocked for 3 minutes.')}",
        status_code=303,
    )
    response.set_cookie(
        DB_INSPECTOR_COOKIE,
        token,
        max_age=web_system_db_inspector_service.CONFIRM_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=True,
        path="/admin/system/tools/db-inspector",
    )
    return response


@router.post(
    "/tools/db-inspector/revoke",
    dependencies=[Depends(require_permission("system:db_admin"))],
)
def db_inspector_revoke():
    response = RedirectResponse(
        url="/admin/system/tools/db-inspector?notice=Access%20revoked",
        status_code=303,
    )
    response.delete_cookie(DB_INSPECTOR_COOKIE, path="/admin/system/tools/db-inspector")
    return response


@router.post(
    "/tools/db-inspector/query",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:db_admin"))],
)
def db_inspector_query(
    request: Request,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    principal_id = _dbi_principal_id(request)
    confirmed = web_system_db_inspector_service.validate_confirmation_token(
        principal_id=principal_id,
        token=request.cookies.get(DB_INSPECTOR_COOKIE),
    )
    if not confirmed:
        return RedirectResponse(
            url="/admin/system/tools/db-inspector?error=Confirm%20password%20to%20unlock%20DB%20Inspector",
            status_code=303,
        )

    form = parse_form_data_sync(request)
    query_text = str(form.get("query_text") or "")
    table = str(form.get("table") or "").strip()
    row_limit_raw = str(form.get("row_limit") or "").strip()
    try:
        row_limit = (
            int(row_limit_raw)
            if row_limit_raw
            else web_system_db_inspector_service.DEFAULT_ROWS
        )
    except ValueError:
        row_limit = web_system_db_inspector_service.DEFAULT_ROWS

    query_result = None
    error = None
    try:
        query_result = web_system_db_inspector_service.run_select_query(
            db,
            query_text,
            row_limit=row_limit,
        )
        log_audit_event(
            db=db,
            request=request,
            action="db_inspector_query",
            entity_type="db_inspector",
            entity_id=None,
            actor_id=principal_id,
            metadata={
                "query": query_result["query"],
                "row_count": query_result["row_count"],
                "row_limit": query_result["row_limit"],
            },
        )
    except Exception as exc:
        error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)

    overview = web_system_db_inspector_service.build_overview(db, selected_table=table)
    return templates.TemplateResponse(
        "admin/system/db_inspector.html",
        {
            "request": request,
            "active_page": "system-db-inspector",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "confirmed": True,
            "query_result": query_result,
            "query_text": query_text,
            "row_limit": row_limit,
            "error": error,
            "notice": None,
            **overview,
        },
    )


@router.post(
    "/tools/db-inspector/export.csv",
    dependencies=[Depends(require_permission("system:db_admin"))],
)
def db_inspector_export_csv(
    request: Request,
    db: Session = Depends(get_db),
):
    principal_id = _dbi_principal_id(request)
    confirmed = web_system_db_inspector_service.validate_confirmation_token(
        principal_id=principal_id,
        token=request.cookies.get(DB_INSPECTOR_COOKIE),
    )
    if not confirmed:
        return RedirectResponse(
            url="/admin/system/tools/db-inspector?error=Confirm%20password%20to%20unlock%20DB%20Inspector",
            status_code=303,
        )

    form = parse_form_data_sync(request)
    query_text = str(form.get("query_text") or "")
    row_limit_raw = str(form.get("row_limit") or "").strip()
    try:
        row_limit = (
            int(row_limit_raw)
            if row_limit_raw
            else web_system_db_inspector_service.DEFAULT_ROWS
        )
    except ValueError:
        row_limit = web_system_db_inspector_service.DEFAULT_ROWS
    result_payload = web_system_db_inspector_service.run_select_query(
        db,
        query_text,
        row_limit=row_limit,
    )
    csv_content = web_system_db_inspector_service.query_result_to_csv(result_payload)
    log_audit_event(
        db=db,
        request=request,
        action="db_inspector_export_csv",
        entity_type="db_inspector",
        entity_id=None,
        actor_id=principal_id,
        metadata={
            "query": result_payload["query"],
            "row_count": result_payload["row_count"],
            "row_limit": result_payload["row_limit"],
        },
    )
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="db_inspector_query.csv"'
        },
    )


# ===================================================================
# Settings Hub, Company Info, About, Logs, and Config Routes
# (04_administration + 08_config feature implementations)
# ===================================================================


def _config_context(request: Request, db: Session, extra: dict) -> dict:
    """Build standard context for config pages."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


# --- Settings Hub ---
@router.get("/settings-hub", response_class=HTMLResponse)
def settings_hub(request: Request, db: Session = Depends(get_db)):
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, dict):
        load_permission_keys(auth, db)
    data = web_system_settings_hub_service.build_settings_hub_context(db)
    valid_category_ids = {str(cat.get("id")) for cat in data.get("categories", [])}
    selected_category = str(request.query_params.get("category") or "").strip()
    if selected_category not in valid_category_ids:
        selected_category = ""
    return templates.TemplateResponse(
        "admin/system/settings_hub.html",
        _config_context(
            request,
            db,
            {
                "active_page": "billing-setup"
                if selected_category == "billing"
                else "settings-hub",
                "selected_category": selected_category,
                **data,
            },
        ),
    )


@router.get(
    "/control-plane",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def control_plane(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/system/control_plane.html",
        _config_context(
            request,
            db,
            {
                "active_page": "control-plane",
                **web_control_plane_service.build_control_plane_context(db),
            },
        ),
    )


@router.get(
    "/ticket-settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def ticket_settings_page(request: Request, db: Session = Depends(get_db)):
    staff_options = support_service.list_assignment_people(db)
    ticket_settings = {
        "active_page": "ticket-settings",
        "status_options": support_ticket_settings_service.list_status_options(db),
        "priority_options": support_ticket_settings_service.list_priority_options(db),
        "ticket_type_options": support_ticket_settings_service.list_ticket_type_options(
            db
        ),
        "region_options": support_ticket_settings_service.list_region_options(db),
        "service_team_options": support_ticket_settings_service.list_service_teams(db),
        "auto_assign_enabled": support_ticket_settings_service.auto_assign_enabled(db),
        "auto_assign_max_open_tickets": support_ticket_settings_service.auto_assign_max_open_tickets(
            db
        ),
        "routing_rules": support_ticket_settings_service.region_assignment_rules(db),
        "sla_policy": support_ticket_settings_service.sla_policy(db),
        "ticket_type_sla_policy": support_ticket_settings_service.ticket_type_sla_policy(
            db
        ),
        "staff_options": staff_options,
        "assignment_rules": support_ticket_settings_service.list_assignment_rules(db),
        "assignment_strategies": ["round_robin", "least_loaded"],
        "assignment_targets": [
            "technician",
            "technical_supervisor",
            "site_coordinator",
        ],
        "saved": request.query_params.get("saved") == "1",
        "rule_saved": request.query_params.get("rule_saved") == "1",
        "rule_deleted": request.query_params.get("rule_deleted") == "1",
        "errors": [],
    }
    return templates.TemplateResponse(
        "admin/system/ticket_settings.html",
        _config_context(request, db, ticket_settings),
    )


@router.post(
    "/ticket-settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def ticket_settings_update(
    request: Request,
    form=Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    statuses = form.getlist("status_values")
    priorities = form.getlist("priority_values")
    ticket_types = form.getlist("ticket_type_values")
    regions = form.getlist("region_values")
    routing_regions = form.getlist("routing_regions")
    routing_ticket_manager_person_ids = form.getlist(
        "routing_ticket_manager_person_ids"
    )
    routing_site_coordinator_person_ids = form.getlist(
        "routing_site_coordinator_person_ids"
    )
    routing_technician_person_ids = form.getlist("routing_technician_person_ids")
    routing_service_team_ids = form.getlist("routing_service_team_ids")
    routing_assignee_person_ids = form.getlist("routing_assignee_person_ids")
    sla_priorities = form.getlist("sla_priorities")
    sla_response_hours = form.getlist("sla_response_hours")
    sla_resolution_hours = form.getlist("sla_resolution_hours")
    sla_aging_hours = form.getlist("sla_aging_hours")
    sla_ticket_types = form.getlist("sla_ticket_types")
    sla_ticket_type_resolution_hours = form.getlist("sla_ticket_type_resolution_hours")
    errors: list[str] = []
    try:
        db_session_adapter.release_read_transaction(db)
        support_ticket_settings_service.update_options(
            db,
            statuses=statuses,
            priorities=priorities,
            ticket_types=ticket_types,
            regions=regions,
            auto_assign=form.get("auto_assign_enabled") == "1",
            auto_assign_max_open_tickets=form.get("auto_assign_max_open_tickets"),
            routing_regions=routing_regions,
            routing_ticket_manager_person_ids=routing_ticket_manager_person_ids,
            routing_site_coordinator_person_ids=routing_site_coordinator_person_ids,
            routing_technician_person_ids=routing_technician_person_ids,
            routing_service_team_ids=routing_service_team_ids,
            routing_assignee_person_ids=routing_assignee_person_ids,
            sla_priorities=sla_priorities,
            sla_response_hours=sla_response_hours,
            sla_resolution_hours=sla_resolution_hours,
            sla_aging_hours=sla_aging_hours,
            sla_ticket_types=sla_ticket_types,
            sla_ticket_type_resolution_hours=sla_ticket_type_resolution_hours,
        )
        return RedirectResponse(
            url="/admin/system/ticket-settings?saved=1", status_code=303
        )
    except Exception as exc:
        errors.append(str(exc) or "Could not save ticket settings.")
    return templates.TemplateResponse(
        "admin/system/ticket_settings.html",
        _config_context(
            request,
            db,
            {
                "active_page": "ticket-settings",
                "status_options": support_ticket_settings_service.list_status_options(
                    db
                ),
                "priority_options": support_ticket_settings_service.list_priority_options(
                    db
                ),
                "ticket_type_options": support_ticket_settings_service.list_ticket_type_options(
                    db
                ),
                "region_options": support_ticket_settings_service.list_region_options(
                    db
                ),
                "service_team_options": support_ticket_settings_service.list_service_teams(
                    db
                ),
                "auto_assign_enabled": support_ticket_settings_service.auto_assign_enabled(
                    db
                ),
                "auto_assign_max_open_tickets": support_ticket_settings_service.auto_assign_max_open_tickets(
                    db
                ),
                "routing_rules": support_ticket_settings_service.region_assignment_rules(
                    db
                ),
                "sla_policy": support_ticket_settings_service.sla_policy(db),
                "ticket_type_sla_policy": support_ticket_settings_service.ticket_type_sla_policy(
                    db
                ),
                "staff_options": support_service.list_assignment_people(db),
                "assignment_rules": support_ticket_settings_service.list_assignment_rules(
                    db
                ),
                "assignment_strategies": ["round_robin", "least_loaded"],
                "assignment_targets": [
                    "technician",
                    "technical_supervisor",
                    "site_coordinator",
                ],
                "saved": False,
                "rule_saved": False,
                "rule_deleted": False,
                "errors": errors,
            },
        ),
        status_code=400,
    )


def _split_form_values(values: list[str]) -> list[str]:
    parsed: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            text = item.strip()
            if text and text not in parsed:
                parsed.append(text)
    return parsed


@router.post(
    "/ticket-settings/assignment-rules",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def ticket_assignment_rule_create(
    name: str = Form(...),
    priority: str = Form("0"),
    strategy: str = Form("round_robin"),
    team_id: str | None = Form(default=None),
    ticket_types: list[str] = Form(default=[]),
    regions: list[str] = Form(default=[]),
    assignee_person_id: str | None = Form(default=None),
    assignment_target: str = Form("technician"),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    support_ticket_settings_service.create_assignment_rule(
        db,
        name=name,
        priority=priority,
        strategy=strategy,
        team_id=team_id,
        ticket_types=_split_form_values(ticket_types),
        regions=_split_form_values(regions),
        assignee_person_id=assignee_person_id,
        assignment_target=assignment_target,
        is_active=is_active == "1",
    )
    return RedirectResponse(
        url="/admin/system/ticket-settings?rule_saved=1", status_code=303
    )


@router.post(
    "/ticket-settings/assignment-rules/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def ticket_assignment_rule_delete(rule_id: str, db: Session = Depends(get_db)):
    db_session_adapter.release_read_transaction(db)
    support_ticket_settings_service.delete_assignment_rule(db, rule_id)
    return RedirectResponse(
        url="/admin/system/ticket-settings?rule_deleted=1", status_code=303
    )


@router.get(
    "/branding",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def branding_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_settings_views_service.build_settings_context(
        db, web_system_settings_views_service.BRANDING_DOMAIN
    )
    return templates.TemplateResponse(
        "admin/system/branding.html",
        _config_context(request, db, {"active_page": "system-branding", **data}),
    )


@router.get(
    "/control-relationships",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def control_relationship_diagnostics(db: Session = Depends(get_db)):
    from app.services.control_relationships import (
        audit_event_relationships,
        audit_setting_relationships,
        event_policies,
        event_topology,
        relationship_manifest,
    )

    return {
        "relationships": relationship_manifest(),
        "event_topology": event_topology(),
        "event_policies": event_policies(),
        "settings_findings": [
            finding.to_dict() for finding in audit_setting_relationships(db)
        ],
        "event_findings": [
            finding.to_dict() for finding in audit_event_relationships()
        ],
    }


@router.get(
    "/email",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def email_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/system/email.html",
        _smtp_page_context(
            request, db, {"saved": request.query_params.get("saved") == "1"}
        ),
    )


# --- Company Info ---
@router.get("/company-info", response_class=HTMLResponse)
def company_info_page(request: Request, db: Session = Depends(get_db)):
    info = web_system_company_info_service.get_company_info(db)
    return templates.TemplateResponse(
        "admin/system/company_info.html",
        _config_context(request, db, {"active_page": "company-info", "info": info}),
    )


@router.post(
    "/company-info",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def company_info_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    web_system_company_info_service.save_company_info(db, form)
    return RedirectResponse(url="/admin/system/company-info", status_code=303)


# --- System About ---
@router.get("/about", response_class=HTMLResponse)
def system_about_page(request: Request, db: Session = Depends(get_db)):
    info = web_system_about_service.get_system_info(db)
    return templates.TemplateResponse(
        "admin/system/about.html",
        _config_context(request, db, {"active_page": "system-about", "info": info}),
    )


# ===================================================================
# Configuration Page Routes (08_config features)
# ===================================================================


# --- 8.4 Notification Templates ---
@router.get("/config/templates", response_class=HTMLResponse)
def config_templates_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_templates_context(db)
    return templates.TemplateResponse(
        "admin/system/config/templates.html",
        _config_context(request, db, {"active_page": "config-templates", **data}),
    )


# --- 8.5 System Preferences ---
@router.get("/config/preferences", response_class=HTMLResponse)
def config_preferences_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_preferences_context(db)
    return templates.TemplateResponse(
        "admin/system/config/preferences.html",
        _config_context(request, db, {"active_page": "config-preferences", **data}),
    )


@router.post(
    "/config/preferences",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_preferences_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    web_system_config_service.save_preferences(db, form)
    return RedirectResponse(url="/admin/system/config/preferences", status_code=303)


# --- 8.9 Email / SMTP ---
@router.get("/config/email", response_class=HTMLResponse)
def config_email_page(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(url="/admin/system/email", status_code=303)


@router.post(
    "/config/email",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_email_save(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(url="/admin/system/email", status_code=303)


# --- 8.7 Subscriber Settings: REMOVED (inert/dead config; subscriber defaults
# are governed by feature-specific workflows) ---


# --- 8.8 Customer Portal ---
@router.get("/config/portal", response_class=HTMLResponse)
def config_portal_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_portal_config_context(db)
    data.update(
        {
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse(
        "admin/system/config/portal.html",
        _config_context(request, db, {"active_page": "config-portal", **data}),
    )


@router.post(
    "/config/portal",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_portal_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    try:
        web_system_config_service.save_portal_config(db, form)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/system/config/portal?error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/system/config/portal?saved=1", status_code=303)


# --- 8.10 Data Retention: REMOVED (inert/dead config; enforced retention lives on
# feature-specific settings such as monitoring, bandwidth, restore, and backups) ---


# --- 8.11 Finance Automation: REMOVED (inert/dead config; billing automation is
# governed by the single control plane — control_registry / module_manager) ---


# --- 8.12 Billing Settings ---
@router.get("/config/billing", response_class=HTMLResponse)
def config_billing_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_billing_config_context(db)
    return templates.TemplateResponse(
        "admin/system/config/billing.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-billing",
                "audit_items": _billing_config_audit_items(db),
                **data,
            },
        ),
    )


@router.post(
    "/config/billing",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_billing_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(web_system_config_service.get_billing_config_context(db)["billing"])
    try:
        web_system_config_service.save_billing_config(db, form)
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/system/config/billing.html",
            _config_context(
                request,
                db,
                {
                    "active_page": "config-billing",
                    "billing": form,
                    "audit_items": _billing_config_audit_items(db),
                    "error": str(exc),
                },
            ),
            status_code=400,
        )
    after = dict(web_system_config_service.get_billing_config_context(db)["billing"])
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="billing_config",
            entity_id="billing",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(url="/admin/system/config/billing", status_code=303)


@router.get("/config/direct-bank-transfer", response_class=HTMLResponse)
def config_direct_bank_transfer_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_direct_bank_transfer_context(db)
    return templates.TemplateResponse(
        "admin/system/config/direct_bank_transfer.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-direct-bank-transfer",
                "audit_items": _direct_bank_transfer_audit_items(db),
                "saved": request.query_params.get("saved") == "1",
                **data,
            },
        ),
    )


@router.post(
    "/config/direct-bank-transfer",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_direct_bank_transfer_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before_context = web_system_config_service.get_direct_bank_transfer_context(db)
    before = {
        **dict(before_context["direct_bank_transfer"]),
        "collection_accounts": before_context["collection_accounts"],
    }
    web_system_config_service.save_direct_bank_transfer_config(db, form)
    after_context = web_system_config_service.get_direct_bank_transfer_context(db)
    after = {
        **dict(after_context["direct_bank_transfer"]),
        "collection_accounts": after_context["collection_accounts"],
    }
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="direct_bank_transfer_config",
            entity_id="direct_bank_transfer",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(
        url="/admin/system/config/direct-bank-transfer?saved=1", status_code=303
    )


# --- 8.13 Payment Methods ---
@router.get("/config/payment-methods", response_class=HTMLResponse)
def config_payment_methods_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_payment_methods_context(db)
    return templates.TemplateResponse(
        "admin/system/config/payment_methods.html",
        _config_context(request, db, {"active_page": "config-payment-methods", **data}),
    )


# --- 8.18 Tax Configuration ---
@router.get("/config/tax", response_class=HTMLResponse)
def config_tax_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_tax_config_context(db)
    return templates.TemplateResponse(
        "admin/system/config/tax.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-tax",
                "audit_items": _tax_rate_audit_items(db),
                **data,
            },
        ),
    )


# --- Automated outage notification (ADR 0004) ---
@router.get("/config/outage-notifications", response_class=HTMLResponse)
def config_outage_auto_notify_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_outage_auto_notify_context(db)
    return templates.TemplateResponse(
        "admin/system/config/outage_notifications.html",
        _config_context(
            request,
            db,
            {"active_page": "config-outage-notifications", **data},
        ),
    )


@router.post(
    "/config/outage-notifications",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_outage_auto_notify_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(
        web_system_config_service.get_outage_auto_notify_context(db)[
            "outage_auto_notify"
        ]
    )
    web_system_config_service.save_outage_auto_notify(db, form)
    after = dict(
        web_system_config_service.get_outage_auto_notify_context(db)[
            "outage_auto_notify"
        ]
    )
    changes = _diff_audit_snapshots(before, after)
    if changes:
        # Arming or disarming customer outage notification is an operational
        # decision worth an audit trail of its own.
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="outage_auto_notify_config",
            entity_id="outage_auto_notify",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(
        url="/admin/system/config/outage-notifications", status_code=303
    )


# --- 8.16 Billing Reminders ---
@router.get("/config/reminders", response_class=HTMLResponse)
def config_reminders_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_reminders_context(db)
    return templates.TemplateResponse(
        "admin/system/config/reminders.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-reminders",
                "audit_items": _billing_reminder_audit_items(db),
                **data,
            },
        ),
    )


@router.post(
    "/config/reminders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_reminders_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(web_system_config_service.get_reminders_context(db)["reminders"])
    web_system_config_service.save_reminders(db, form)
    after = dict(web_system_config_service.get_reminders_context(db)["reminders"])
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="billing_reminder_config",
            entity_id="reminders",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(url="/admin/system/config/reminders", status_code=303)


# --- 8.17 Billing Notifications ---
@router.get("/config/billing-notifications", response_class=HTMLResponse)
def config_billing_notif_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_billing_notifications_context(db)
    return templates.TemplateResponse(
        "admin/system/config/billing_notifications.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-billing-notif",
                "audit_items": _billing_notification_audit_items(db),
                **data,
            },
        ),
    )


@router.post(
    "/config/billing-notifications",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_billing_notif_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(
        web_system_config_service.get_billing_notifications_context(db)["billing_notif"]
    )
    web_system_config_service.save_billing_notifications(db, form)
    after = dict(
        web_system_config_service.get_billing_notifications_context(db)["billing_notif"]
    )
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="billing_notification_config",
            entity_id="billing_notifications",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(
        url="/admin/system/config/billing-notifications", status_code=303
    )


# --- 8.19 Plan Change ---
@router.get("/config/plan-change", response_class=HTMLResponse)
def config_plan_change_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_plan_change_context(db)
    return templates.TemplateResponse(
        "admin/system/config/plan_change.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-plan-change",
                "audit_items": _plan_change_audit_items(db),
                **data,
            },
        ),
    )


@router.post(
    "/config/plan-change",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_plan_change_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(web_system_config_service.get_plan_change_context(db)["plan_change"])
    try:
        web_system_config_service.save_plan_change(db, form)
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/system/config/plan_change.html",
            _config_context(
                request,
                db,
                {
                    "active_page": "config-plan-change",
                    "plan_change": form,
                    "audit_items": _plan_change_audit_items(db),
                    "error": str(exc),
                },
            ),
            status_code=400,
        )
    after = dict(web_system_config_service.get_plan_change_context(db)["plan_change"])
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="plan_change_config",
            entity_id="plan_change",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    return RedirectResponse(url="/admin/system/config/plan-change", status_code=303)


# --- 8.20 RADIUS Configuration ---
@router.get("/config/radius", response_class=HTMLResponse)
def config_radius_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_radius_config_context(db)
    push_status = str(request.query_params.get("push_status") or "").strip()
    push_message = str(request.query_params.get("push_message") or "").strip()
    saved = request.query_params.get("saved") == "1"
    error = str(request.query_params.get("error") or "").strip()
    return templates.TemplateResponse(
        "admin/system/config/radius.html",
        _config_context(
            request,
            db,
            {
                "active_page": "config-radius",
                "push_status": push_status,
                "push_message": push_message,
                "saved": saved,
                "error": error,
                "audit_items": _radius_config_audit_items(db),
                **data,
            },
        ),
    )


@router.post(
    "/config/radius",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_radius_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    before = dict(web_system_config_service.get_radius_config_context(db)["radius"])
    try:
        web_system_config_service.save_radius_config(db, form)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/system/config/radius?error={quote_plus(str(exc))}",
            status_code=303,
        )
    after = dict(web_system_config_service.get_radius_config_context(db)["radius"])
    changes = _diff_audit_snapshots(before, after)
    if changes:
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_config",
            entity_id="radius",
            actor_id=_system_actor_id(request),
            metadata={"changes": changes},
        )
    try:
        radius_reject_service.push_reject_rules_once(db)
    except Exception:
        # Do not block settings persistence if router push fails.
        logger.warning(
            "Failed to push RADIUS reject rules after config save", exc_info=True
        )
    return RedirectResponse(url="/admin/system/config/radius?saved=1", status_code=303)


@router.post(
    "/config/radius/push-reject-rules",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_radius_push_reject_rules(request: Request, db: Session = Depends(get_db)):
    result = radius_reject_service.push_reject_rules_to_radius_nas(db)
    status = "success" if result.get("ok") else "error"
    message = quote_plus(str(result.get("message") or "Reject rule push failed."))
    log_audit_event(
        db=db,
        request=request,
        action="push_reject_rules",
        entity_type="radius_config",
        entity_id="radius",
        actor_id=_system_actor_id(request),
        metadata={
            "changes": {
                "reject_rules_push": {
                    "from": None,
                    "to": str(result.get("message") or status),
                }
            },
            "success": bool(result.get("ok")),
        },
        is_success=bool(result.get("ok")),
    )
    return RedirectResponse(
        url=f"/admin/system/config/radius?push_status={status}&push_message={message}",
        status_code=303,
    )


# --- 8.22 CPE Configuration: REMOVED (inert/dead config; no active consumer) ---


# --- 8.23 Monitoring ---
@router.get("/config/monitoring", response_class=HTMLResponse)
def config_monitoring_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_monitoring_config_context(db)
    data.update(
        {
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse(
        "admin/system/config/monitoring.html",
        _config_context(request, db, {"active_page": "config-monitoring", **data}),
    )


@router.post(
    "/config/monitoring",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def config_monitoring_save(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    try:
        web_system_config_service.save_monitoring_config(db, form)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/system/config/monitoring?error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/system/config/monitoring?saved=1", status_code=303
    )


# --- 8.25 FUP: REMOVED (inert/dead config; no active enforcement consumer) ---


# --- 8.26 NAS Types ---
@router.get("/config/nas-types", response_class=HTMLResponse)
def config_nas_types_page(request: Request, db: Session = Depends(get_db)):
    data = web_system_config_service.get_nas_types_context(db)
    return templates.TemplateResponse(
        "admin/system/config/nas_types.html",
        _config_context(request, db, {"active_page": "config-nas-types", **data}),
    )


# --- 8.27 IPv6: REMOVED (inert/dead config; no provisioning/IPAM consumer) ---


# ── Secrets Management (OpenBao) ─────────────────────────────────────


@router.get(
    "/secrets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:read"))],
)
def secrets_management(
    request: Request,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    """OpenBao secrets management dashboard."""
    ctx = _system_page_context(request, db, "Secrets", "secrets")
    ctx.update(
        web_system_secrets_service.build_secrets_index_context(
            status=status,
            message=message,
        )
    )
    return templates.TemplateResponse("admin/system/secrets.html", ctx)


@router.get(
    "/secrets/{path:path}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:read"))],
)
def secrets_edit(request: Request, path: str, db: Session = Depends(get_db)):
    """Edit a secret's fields."""
    state = web_system_secrets_service.build_secret_edit_context(path)
    if state.get("redirect_url"):
        return RedirectResponse(str(state["redirect_url"]), status_code=303)
    ctx = _system_page_context(request, db, "Secrets", "secrets")
    ctx.update(state)
    return templates.TemplateResponse("admin/system/secrets_edit.html", ctx)


@router.post(
    "/secrets/{path:path}/save",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:write"))],
)
def secrets_save(request: Request, path: str, db: Session = Depends(get_db)):
    """Save updated secret fields."""
    form = parse_form_data_sync(request)
    result = web_system_secrets_service.save_secret(path, form)
    if result.get("ok"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    ctx = _system_page_context(request, db, "Secrets", "secrets")
    ctx.update(result)
    return templates.TemplateResponse("admin/system/secrets_edit.html", ctx)


@router.get(
    "/secrets/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:read"))],
)
def secrets_new(request: Request, db: Session = Depends(get_db)):
    """Create a new secret."""
    ctx = _system_page_context(request, db, "Secrets", "secrets")
    ctx.update(web_system_secrets_service.build_secret_new_context())
    return templates.TemplateResponse("admin/system/secrets_new.html", ctx)


@router.post(
    "/secrets/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:write"))],
)
def secrets_create(request: Request, db: Session = Depends(get_db)):
    """Create a new secret path with fields."""
    form = parse_form_data_sync(request)
    result = web_system_secrets_service.create_secret(form)
    if result.get("ok"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    if result.get("error"):
        ctx = _system_page_context(request, db, "Secrets", "secrets")
        ctx["error"] = result["error"]
        return templates.TemplateResponse("admin/system/secrets_new.html", ctx)
    ctx = _system_page_context(request, db, "Secrets", "secrets")
    ctx["error"] = "Failed to create secret in OpenBao"
    return templates.TemplateResponse("admin/system/secrets_new.html", ctx)


@router.post(
    "/secrets/{path:path}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:secrets:write"))],
)
def secrets_delete(request: Request, path: str, db: Session = Depends(get_db)):
    """Delete a secret."""
    return RedirectResponse(
        web_system_secrets_service.delete_secret_path(path),
        status_code=303,
    )


logger = logging.getLogger(__name__)


def _qr_code_data_url(value: str) -> str:
    image = qrcode.make(value)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
