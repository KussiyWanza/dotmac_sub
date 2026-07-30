"""Typed WhatsApp capability facade with no settings or legacy fallback."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationCapabilityBinding
from app.services.integrations import installations
from app.services.integrations.connectors.whatsapp_runtime import WhatsAppRuntimeRunner
from app.services.integrations.runtime import (
    OperationResult,
    OperationStatus,
    OperationTrigger,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionContext,
    build_execution_context,
    make_operation_executor,
)
from app.services.secrets import resolve_secret

WHATSAPP_SEND_CAPABILITY = "messaging.send.v1"
WHATSAPP_RECEIVE_CAPABILITY = "messaging.receive.v1"
WHATSAPP_TEMPLATE_READ_CAPABILITY = "messaging.templates.read.v1"
_TEMPLATE_CACHE_TTL_SECONDS = 300
_template_list_cache: dict[str, tuple[float, tuple[dict[str, Any], ...]]] = {}


def require_binding(db: Session, *, capability_id: str) -> IntegrationCapabilityBinding:
    return installations.require_enabled_capability_binding(
        db,
        connector_key="whatsapp",
        capability_id=capability_id,
    )


def active_config(
    db: Session, *, capability_id: str = WHATSAPP_SEND_CAPABILITY
) -> dict[str, Any]:
    binding = require_binding(db, capability_id=capability_id)
    revision = binding.installation.current_config_revision
    if revision is None:
        raise installations.InstallationError("WhatsApp configuration revision missing")
    return dict(revision.config_json or {})


def execution_context(
    db: Session,
    *,
    capability_id: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> RuntimeExecutionContext:
    binding = require_binding(db, capability_id=capability_id)
    return build_execution_context(
        db,
        capability_binding_id=binding.id,
        runner_override=WhatsAppRuntimeRunner(),
        secret_resolver=secret_resolver,
    )


def inbound_secret_material(
    db: Session,
    *,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> dict[str, str]:
    return dict(
        execution_context(
            db,
            capability_id=WHATSAPP_RECEIVE_CAPABILITY,
            secret_resolver=secret_resolver,
        ).secret_material
    )


def _typed_result(result: OperationResult) -> dict[str, Any]:
    normalized = dict(result.output)
    normalized["ok"] = result.status == OperationStatus.succeeded
    normalized["operation_status"] = result.status.value
    if result.error_code:
        normalized["error_code"] = result.error_code
        normalized.setdefault("response", result.error_code)
    if result.external_receipt:
        normalized["external_receipt"] = dict(result.external_receipt)
        provider_message_id = result.external_receipt.get("provider_message_id")
        if provider_message_id:
            normalized["provider_message_id"] = provider_message_id
    return normalized


def _dispatch(
    db: Session,
    *,
    action: str,
    params: dict[str, Any],
    dry_run: bool,
    correlation_id: str,
    secret_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    context = execution_context(
        db,
        capability_id=WHATSAPP_SEND_CAPABILITY,
        secret_resolver=secret_resolver,
    )
    executor = make_operation_executor(
        context,
        correlation_id=correlation_id,
        trigger=OperationTrigger.interactive if dry_run else OperationTrigger.event,
        actor="integration.whatsapp",
        timeout_seconds=int(context.config.get("timeout_seconds") or 10) + 5,
    )
    return _typed_result(executor(action, {**params, "preview": dry_run}))


def send_text_message(
    db: Session,
    *,
    recipient: str,
    body: str,
    dry_run: bool = False,
    correlation_id: str | None = None,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> dict[str, Any]:
    correlation = correlation_id or f"whatsapp:text:{recipient}"
    return _dispatch(
        db,
        action="send_text",
        params={"recipient": recipient, "body": body},
        dry_run=dry_run,
        correlation_id=correlation,
        secret_resolver=secret_resolver,
    )


def send_template_message(
    db: Session,
    *,
    recipient: str,
    template_name: str,
    language: str | None = None,
    variables: dict[str, Any] | None = None,
    components: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
    correlation_id: str | None = None,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> dict[str, Any]:
    correlation = correlation_id or f"whatsapp:template:{recipient}:{template_name}"
    return _dispatch(
        db,
        action="send_template",
        params={
            "recipient": recipient,
            "template_name": template_name,
            "language": language or "en",
            "variables": variables or {},
            "components": components or [],
        },
        dry_run=dry_run,
        correlation_id=correlation,
        secret_resolver=secret_resolver,
    )


def fetch_template_details(
    db: Session,
    *,
    template_name: str,
    language: str | None = None,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> dict[str, Any]:
    context = execution_context(
        db,
        capability_id=WHATSAPP_TEMPLATE_READ_CAPABILITY,
        secret_resolver=secret_resolver,
    )
    executor = make_operation_executor(
        context,
        correlation_id=f"whatsapp:template-read:{template_name}:{language or '*'}",
        trigger=OperationTrigger.interactive,
        actor="integration.whatsapp.templates",
        timeout_seconds=int(context.config.get("timeout_seconds") or 10) + 5,
    )
    result = _typed_result(
        executor(
            "get_template",
            {"template_name": template_name, "language": language or ""},
        )
    )
    if not result.get("ok"):
        raise ValueError(str(result.get("error_code") or "template read failed"))
    template = result.get("template")
    if not isinstance(template, dict):
        raise ValueError("template response invalid")
    return template


def list_approved_templates(
    db: Session,
    *,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> tuple[dict[str, Any], ...]:
    binding = require_binding(db, capability_id=WHATSAPP_TEMPLATE_READ_CAPABILITY)
    revision = binding.installation.current_config_revision
    cache_key = f"{binding.id}:{revision.id if revision is not None else 'none'}"
    cached = _template_list_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _TEMPLATE_CACHE_TTL_SECONDS:
        return cached[1]

    context = build_execution_context(
        db,
        capability_binding_id=binding.id,
        runner_override=WhatsAppRuntimeRunner(),
        secret_resolver=secret_resolver,
    )
    executor = make_operation_executor(
        context,
        correlation_id=f"whatsapp:template-list:{cache_key}",
        trigger=OperationTrigger.interactive,
        actor="integration.whatsapp.templates",
        timeout_seconds=int(context.config.get("timeout_seconds") or 10) + 5,
    )
    result = _typed_result(executor("list_templates", {}))
    if not result.get("ok"):
        raise ValueError(str(result.get("error_code") or "template list failed"))
    rows = result.get("templates")
    templates = tuple(
        dict(item)
        for item in (rows if isinstance(rows, list) else [])
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() == "approved"
    )
    _template_list_cache[cache_key] = (now, templates)
    return templates
