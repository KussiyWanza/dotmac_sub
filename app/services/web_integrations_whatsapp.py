"""Admin projection for installation-backed WhatsApp configuration."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInstallation
from app.services.integrations import (
    installations,
    runtime_execution,
    whatsapp_capability,
)
from app.services.integrations.connectors.whatsapp_runtime import (
    WHATSAPP_PROVIDER_META,
)
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    WHATSAPP_SEND_CAPABILITY,
    WHATSAPP_TEMPLATE_READ_CAPABILITY,
)
from app.services.secrets import is_secret_ref

_PROVIDER_OPTIONS = [
    {"id": WHATSAPP_PROVIDER_META, "label": "Meta Cloud API"},
]


def _selected_installation(db: Session) -> IntegrationInstallation | None:
    rows = installations.list_installations(db, connector_key="whatsapp", limit=200)
    if not rows:
        return None
    defaults = [
        row
        for row in rows
        if any(
            binding.capability_id == WHATSAPP_SEND_CAPABILITY
            and (binding.policy_json or {}).get("default") is True
            for binding in row.capability_bindings
        )
    ]
    if len(defaults) == 1:
        return defaults[0]
    if len(rows) == 1:
        return rows[0]
    raise ValueError(
        "Multiple WhatsApp installations exist; select one in Integrations"
    )


def _masked_reference(reference: str) -> str:
    if not reference:
        return ""
    if len(reference) <= 12:
        return "*" * len(reference)
    return f"{reference[:6]}…{reference[-4:]}"


def build_config_state(db: Session) -> dict[str, Any]:
    installation = _selected_installation(db)
    revision = installation.current_config_revision if installation else None
    config = dict(revision.config_json or {}) if revision else {}
    refs = dict(revision.secret_refs or {}) if revision else {}
    return {
        "provider_options": _PROVIDER_OPTIONS,
        "installation": installation,
        "form": {
            "provider": config.get("provider", WHATSAPP_PROVIDER_META),
            "phone_number": config.get("phone_number", ""),
            "phone_number_id": config.get("phone_number_id", ""),
            "waba_id": config.get("waba_id", ""),
            "webhook_url": config.get("webhook_url", ""),
            "graph_version": config.get("graph_version", "v21.0"),
            "api_key": "",
            "api_secret": "",  # nosec - reference input, never secret material
            "webhook_verify_token": "",
            "api_key_masked": _masked_reference(
                str(refs.get("service_credentials") or "")
            ),
            "api_secret_masked": _masked_reference(
                str(refs.get("webhook_signing_secret") or "")
            ),
            "webhook_verify_token_masked": _masked_reference(
                str(refs.get("webhook_verify_token") or "")
            ),
            "message_templates_json": json.dumps(config.get("templates", []), indent=2),
        },
    }


def _secret_reference(value: str, existing: str | None, *, label: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return existing
    if not is_secret_ref(candidate):
        raise ValueError(f"{label} must be an OpenBao or env secret reference")
    return candidate


def save_config(
    db: Session,
    *,
    provider: str,
    phone_number: str,
    phone_number_id: str,
    webhook_url: str,
    api_key: str,
    api_secret: str,
    message_templates_json: str,
    waba_id: str = "",
    graph_version: str = "v21.0",
    webhook_verify_token: str = "",
    actor: str = "admin.whatsapp",
) -> IntegrationInstallation:
    provider_value = provider.strip().lower()
    if provider_value not in {option["id"] for option in _PROVIDER_OPTIONS}:
        raise ValueError("Unsupported WhatsApp API provider")
    if not phone_number_id.strip():
        raise ValueError("WhatsApp phone number ID is required")
    try:
        templates = json.loads(message_templates_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Message templates must be valid JSON") from exc
    if not isinstance(templates, list) or any(
        not isinstance(item, dict) for item in templates
    ):
        raise ValueError("Message templates must be a JSON array of objects")

    installation = _selected_installation(db)
    if installation is None:
        installation = installations.create_draft(
            db,
            connector_key="whatsapp",
            name=f"WhatsApp {uuid4().hex[:8]}",
            actor=actor,
        )
    current = installation.current_config_revision
    existing_refs = dict(current.secret_refs or {}) if current else {}
    service_credentials = _secret_reference(
        api_key,
        existing_refs.get("service_credentials"),
        label="Service credential",
    )
    if not service_credentials:
        raise ValueError("Service credential secret reference is required")
    secret_refs = {"service_credentials": service_credentials}
    for name, candidate, label in (
        ("webhook_signing_secret", api_secret, "Webhook signing secret"),
        ("webhook_verify_token", webhook_verify_token, "Webhook verify token"),
    ):
        reference = _secret_reference(
            candidate,
            existing_refs.get(name),
            label=label,
        )
        if reference:
            secret_refs[name] = reference
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={
            "provider": provider_value,
            "phone_number": phone_number.strip(),
            "phone_number_id": phone_number_id.strip(),
            "waba_id": waba_id.strip(),
            "webhook_url": webhook_url.strip(),
            "graph_version": graph_version.strip() or "v21.0",
            "timeout_seconds": 10,
            "templates": templates,
        },
        secret_refs=secret_refs,
        actor=actor,
    )
    for capability_id in (
        WHATSAPP_SEND_CAPABILITY,
        WHATSAPP_RECEIVE_CAPABILITY,
        WHATSAPP_TEMPLATE_READ_CAPABILITY,
    ):
        installations.bind_capability(
            db,
            installation_id=installation.id,
            capability_id=capability_id,
            scope={
                "channel": "whatsapp",
                "phone_number_id": phone_number_id.strip(),
            },
            policy={"default": True},
            actor=actor,
        )
    static_result = installations.validate_static(
        db, installation_id=installation.id, actor=actor
    )
    if not static_result.valid:
        raise ValueError(
            "WhatsApp static validation failed: " + ",".join(static_result.error_codes)
        )
    send_binding = next(
        binding
        for binding in installation.capability_bindings
        if binding.capability_id == WHATSAPP_SEND_CAPABILITY
    )
    runtime_result = runtime_execution.validate_connection(
        runtime_execution.build_execution_context(
            db, capability_binding_id=send_binding.id, allow_disabled=True
        )
    )
    if not runtime_result.valid:
        raise ValueError(
            "WhatsApp runtime validation failed: "
            + ",".join(runtime_result.error_codes)
        )
    return installations.enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=runtime_result,
        actor=actor,
    )


def run_test_send(
    db: Session,
    *,
    recipient: str,
    template_name: str,
    variables_json: str,
) -> dict[str, Any]:
    try:
        variables = json.loads(variables_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Variables must be valid JSON") from exc
    if not isinstance(variables, dict):
        raise ValueError("Variables must be a JSON object")
    return whatsapp_capability.send_template_message(
        db,
        recipient=recipient.strip(),
        template_name=template_name.strip(),
        variables=variables,
        dry_run=True,
    )
