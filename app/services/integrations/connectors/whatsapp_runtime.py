"""Database-free WhatsApp transport for the typed integration runtime."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

WHATSAPP_SEND_CAPABILITY = "messaging.send.v1"
WHATSAPP_RECEIVE_CAPABILITY = "messaging.receive.v1"
WHATSAPP_TEMPLATE_READ_CAPABILITY = "messaging.templates.read.v1"
WHATSAPP_PROVIDER_META = "meta_cloud_api"
SUPPORTED_WHATSAPP_PROVIDERS = frozenset({WHATSAPP_PROVIDER_META})
_TEMPLATE_VARIABLE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def _provider(config: Mapping[str, Any]) -> str:
    return str(config.get("provider") or "").strip()


def _graph_version(config: Mapping[str, Any]) -> str:
    version = str(config.get("graph_version") or "v21.0").strip() or "v21.0"
    return version if version.startswith("v") else f"v{version}"


def _ordered_template_parameters(variables: Mapping[str, Any]) -> list[str]:
    ordered: list[tuple[int, str]] = []
    trailing: list[str] = []
    for key, value in variables.items():
        value_text = "" if value is None else str(value)
        if str(key).strip().isdigit():
            ordered.append((int(str(key).strip()), value_text))
        else:
            trailing.append(value_text)
    return [value for _index, value in sorted(ordered)] + trailing


def build_text_payload(*, provider: str, recipient: str, body: str) -> dict[str, Any]:
    normalized_recipient = recipient.strip()
    if provider == WHATSAPP_PROVIDER_META:
        return {
            "messaging_product": "whatsapp",
            "to": normalized_recipient,
            "type": "text",
            "text": {"body": body},
        }
    raise ValueError("unsupported_whatsapp_provider")


def build_template_payload(
    *,
    provider: str,
    recipient: str,
    template_name: str,
    language: str,
    variables: Mapping[str, Any],
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if provider == WHATSAPP_PROVIDER_META:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": recipient.strip(),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language or "en"},
            },
        }
        parameters = _ordered_template_parameters(variables)
        if components:
            payload["template"]["components"] = components
        elif parameters:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": value} for value in parameters
                    ],
                }
            ]
        return payload
    raise ValueError("unsupported_whatsapp_provider")


def normalize_inbound_webhook(
    *, provider: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Normalize a verified provider fact without persistence or decisions."""

    if provider == WHATSAPP_PROVIDER_META:
        message = payload.get("message") or {}
        sender = message.get("from") or payload.get("from")
        return {
            "provider": provider,
            "from": sender,
            "normalized_from": normalize_phone_identifier(sender),
            "text": message.get("text") or payload.get("text"),
            "external_id": message.get("id") or payload.get("id"),
            "raw": payload,
        }
    raise ValueError("unsupported_whatsapp_provider")


def _endpoint(config: Mapping[str, Any]) -> str:
    provider = _provider(config)
    if provider == WHATSAPP_PROVIDER_META:
        phone_number = str(config.get("phone_number") or "").strip()
        return (
            f"https://graph.facebook.com/{_graph_version(config)}/"
            f"{phone_number}/messages"
        )
    raise ValueError("unsupported_whatsapp_provider")


def _headers(provider: str, credential: str) -> dict[str, str]:
    if provider != WHATSAPP_PROVIDER_META:
        raise ValueError("unsupported_whatsapp_provider")
    return {
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    }


def _response_receipt(response: httpx.Response) -> dict[str, Any]:
    receipt: dict[str, Any] = {"status_code": response.status_code}
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        messages = body.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            external_id = messages[0].get("id")
            if external_id:
                receipt["provider_message_id"] = str(external_id)
        for key in ("sid", "id"):
            if body.get(key):
                receipt["provider_message_id"] = str(body[key])
                break
    return receipt


def _template_variables(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    body = next(
        (
            str(component.get("text") or "")
            for component in components
            if str(component.get("type") or "").upper() == "BODY"
        ),
        "",
    )
    indexes = sorted(
        {int(match.group(1)) for match in _TEMPLATE_VARIABLE_RE.finditer(body)}
    )
    return [
        {
            "index": index,
            "key": str(index),
            "label": f"Field {index}",
            "placeholder": f"Select a source for field {index}",
        }
        for index in indexes
    ]


class WhatsAppRuntimeRunner:
    """Transport-only WhatsApp runner; it receives no Sub persistence handle."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        errors: list[str] = []
        provider = _provider(config)
        if provider not in SUPPORTED_WHATSAPP_PROVIDERS:
            errors.append("provider_unsupported")
        if not str(config.get("phone_number") or "").strip():
            errors.append("phone_number_required")
        if not str(secret_material.get("service_credentials") or "").strip():
            errors.append("service_credentials_required")
        return ValidationResult(valid=not errors, error_codes=tuple(errors))

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        if envelope.capability_id == WHATSAPP_TEMPLATE_READ_CAPABILITY:
            return self._read_template(
                envelope,
                config=config,
                secret_material=secret_material,
            )
        if envelope.capability_id != WHATSAPP_SEND_CAPABILITY:
            return self._rejected(envelope, "capability_unsupported")
        action = str(envelope.payload.get("action") or "")
        params = envelope.payload.get("params")
        if not isinstance(params, dict):
            return self._rejected(envelope, "params_invalid")
        try:
            payload = self._payload(action, config=config, params=params)
        except (TypeError, ValueError) as exc:
            return self._rejected(envelope, str(exc) or "payload_invalid")
        provider = _provider(config)
        preview = bool(params.get("preview"))
        output = {
            "ok": True,
            "provider": provider,
            "sent": False,
            "payload": payload,
        }
        if preview:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                output=output,
            )
        credential = str(secret_material.get("service_credentials") or "").strip()
        if not credential:
            return self._rejected(envelope, "service_credentials_required")
        remaining = max(1.0, (envelope.deadline_at - datetime.now(UTC)).total_seconds())
        configured_timeout = float(config.get("timeout_seconds") or 10)
        timeout = min(max(1.0, configured_timeout), remaining)
        try:
            response = httpx.post(
                _endpoint(config),
                json=payload,
                headers=_headers(provider, credential),
                timeout=timeout,
            )
        except httpx.ConnectTimeout:
            return self._failed(
                envelope, OperationStatus.retryable, "provider_connect_timeout"
            )
        except httpx.TimeoutException:
            return self._failed(
                envelope,
                OperationStatus.reconciliation_required,
                "provider_outcome_ambiguous",
            )
        except httpx.RequestError:
            return self._failed(
                envelope, OperationStatus.retryable, "provider_unavailable"
            )
        receipt = _response_receipt(response)
        output.update(
            {
                "sent": True,
                "status_code": response.status_code,
                "response": response.text[:2000],
            }
        )
        if response.status_code == 429 or response.status_code >= 500:
            output["ok"] = False
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                output=output,
                external_receipt=receipt,
                error_code="provider_retryable_response",
            )
        if response.status_code >= 400:
            output["ok"] = False
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                output=output,
                external_receipt=receipt,
                error_code="provider_rejected_message",
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output=output,
            external_receipt=receipt,
        )

    def _read_template(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        if _provider(config) != WHATSAPP_PROVIDER_META:
            return self._rejected(envelope, "template_read_provider_unsupported")
        action = str(envelope.payload.get("action") or "")
        if action not in {"get_template", "list_templates"}:
            return self._rejected(envelope, "action_unsupported")
        params = envelope.payload.get("params")
        if not isinstance(params, dict):
            return self._rejected(envelope, "params_invalid")
        name = str(params.get("template_name") or "").strip()
        if action == "get_template" and not name:
            return self._rejected(envelope, "template_name_required")
        waba_id = str(config.get("waba_id") or "").strip()
        credential = str(secret_material.get("service_credentials") or "").strip()
        if not waba_id:
            return self._rejected(envelope, "waba_id_required")
        if not credential:
            return self._rejected(envelope, "service_credentials_required")
        url = (
            f"https://graph.facebook.com/{_graph_version(config)}/{waba_id}/"
            "message_templates"
        )
        timeout = min(
            float(config.get("timeout_seconds") or 10),
            max(1.0, (envelope.deadline_at - datetime.now(UTC)).total_seconds()),
        )
        try:
            response = httpx.get(
                url,
                params={
                    "fields": "name,status,language,category,components",
                    "limit": 200,
                    **({"name": name} if name else {}),
                },
                headers={"Authorization": f"Bearer {credential}"},
                timeout=timeout,
            )
        except httpx.HTTPError:
            return self._failed(
                envelope, OperationStatus.retryable, "template_provider_unavailable"
            )
        if response.status_code == 429 or response.status_code >= 500:
            return self._failed(
                envelope, OperationStatus.retryable, "template_provider_retryable"
            )
        if response.status_code >= 400:
            return self._rejected(envelope, "template_provider_rejected")
        try:
            rows = response.json().get("data") or []
        except (AttributeError, ValueError, json.JSONDecodeError):
            return self._rejected(envelope, "template_response_invalid")
        language = str(params.get("language") or "").strip()
        templates: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if action == "get_template" and str(row.get("name") or "") != name:
                continue
            if language and str(row.get("language") or "") != language:
                continue
            components = row.get("components") or []
            if not isinstance(components, list):
                components = []
            body = next(
                (
                    str(component.get("text") or "")
                    for component in components
                    if isinstance(component, dict)
                    and str(component.get("type") or "").upper() == "BODY"
                ),
                "",
            )
            template = {
                "name": row.get("name"),
                "status": row.get("status"),
                "language": row.get("language"),
                "category": row.get("category"),
                "components": components,
                "body": body,
                "variables": _template_variables(
                    [item for item in components if isinstance(item, dict)]
                ),
            }
            templates.append(template)
            if action == "list_templates":
                continue
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                output={"ok": True, "template": template},
            )
        if action == "list_templates":
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                output={"ok": True, "templates": templates},
            )
        return self._rejected(envelope, "template_not_found")

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        result = self.validate(
            manifest=manifest,
            config=config,
            secret_material=secret_material,
        )
        return HealthResult(
            status="configured" if result.valid else "invalid",
            details={"error_codes": list(result.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False

    @staticmethod
    def _payload(
        action: str, *, config: Mapping[str, Any], params: Mapping[str, Any]
    ) -> dict[str, Any]:
        provider = _provider(config)
        recipient = str(params.get("recipient") or "").strip()
        if not recipient:
            raise ValueError("recipient_required")
        if action == "send_text":
            body = str(params.get("body") or "")
            if not body:
                raise ValueError("body_required")
            return build_text_payload(provider=provider, recipient=recipient, body=body)
        if action == "send_template":
            template_name = str(params.get("template_name") or "").strip()
            if not template_name:
                raise ValueError("template_name_required")
            variables = params.get("variables") or {}
            if not isinstance(variables, dict):
                raise ValueError("template_variables_invalid")
            components = params.get("components") or []
            if not isinstance(components, list) or any(
                not isinstance(item, dict) for item in components
            ):
                raise ValueError("template_components_invalid")
            return build_template_payload(
                provider=provider,
                recipient=recipient,
                template_name=template_name,
                language=str(params.get("language") or "en"),
                variables=variables,
                components=components,
            )
        raise ValueError("action_unsupported")

    @staticmethod
    def _rejected(envelope: OperationEnvelope, code: str) -> OperationResult:
        return WhatsAppRuntimeRunner._failed(envelope, OperationStatus.rejected, code)

    @staticmethod
    def _failed(
        envelope: OperationEnvelope, status: OperationStatus, code: str
    ) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id,
            status=status,
            output={"ok": False, "sent": False},
            error_code=code[:120],
        )
