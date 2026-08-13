"""Database-free Facebook Messenger and Instagram Login transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.meta_social_contracts import MetaSocialChannel
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

META_SOCIAL_SEND_CAPABILITY = "messaging.send.v1"
META_SOCIAL_RECEIVE_CAPABILITY = "messaging.receive.v1"
# These public strings identify secret bindings; they are not credential values.
FACEBOOK_TOKEN_BINDING = "facebook_page_access_token"  # nosec B105
INSTAGRAM_TOKEN_BINDING = "instagram_login_access_token"  # nosec B105
META_OAUTH_TOKEN_BINDING = "meta_oauth_access_token"  # nosec B105
WEBHOOK_SIGNING_SECRET_BINDING = "webhook_signing_secret"  # nosec B105
WEBHOOK_VERIFY_TOKEN_BINDING = "webhook_verify_token"  # nosec B105
META_SOCIAL_AUTH_MODE_OAUTH = "oauth"
META_SOCIAL_AUTH_MODE_INDIVIDUAL = "individual"


def _graph_version(config: Mapping[str, Any]) -> str:
    version = str(config.get("graph_version") or "v21.0").strip() or "v21.0"
    return version if version.startswith("v") else f"v{version}"


def _configured_account_id(
    config: Mapping[str, Any], channel: MetaSocialChannel
) -> str:
    key = (
        "facebook_page_id"
        if channel is MetaSocialChannel.facebook_messenger
        else "instagram_account_id"
    )
    return str(config.get(key) or "").strip()


def _token_binding(channel: MetaSocialChannel) -> str:
    return (
        FACEBOOK_TOKEN_BINDING
        if channel is MetaSocialChannel.facebook_messenger
        else INSTAGRAM_TOKEN_BINDING
    )


def _auth_mode(config: Mapping[str, Any]) -> str:
    mode = str(config.get("auth_mode") or META_SOCIAL_AUTH_MODE_INDIVIDUAL).strip()
    if mode in {META_SOCIAL_AUTH_MODE_OAUTH, META_SOCIAL_AUTH_MODE_INDIVIDUAL}:
        return mode
    return META_SOCIAL_AUTH_MODE_INDIVIDUAL


def _credential_binding(config: Mapping[str, Any], channel: MetaSocialChannel) -> str:
    if _auth_mode(config) == META_SOCIAL_AUTH_MODE_OAUTH:
        return META_OAUTH_TOKEN_BINDING
    return _token_binding(channel)


def _endpoint(config: Mapping[str, Any], channel: MetaSocialChannel) -> str:
    version = _graph_version(config)
    account_id = _configured_account_id(config, channel)
    if channel is MetaSocialChannel.facebook_messenger:
        return f"https://graph.facebook.com/{version}/{account_id}/messages"
    if _auth_mode(config) == META_SOCIAL_AUTH_MODE_OAUTH:
        return f"https://graph.facebook.com/{version}/{account_id}/messages"
    return f"https://graph.instagram.com/{version}/me/messages"


def _payload(
    *,
    config: Mapping[str, Any],
    channel: MetaSocialChannel,
    recipient_id: str,
    body: str,
) -> dict[str, Any]:
    message = {"text": body}
    if (
        channel is MetaSocialChannel.instagram_dm
        and _auth_mode(config) == META_SOCIAL_AUTH_MODE_INDIVIDUAL
    ):
        return {
            "recipient": json.dumps({"id": recipient_id}, separators=(",", ":")),
            "message": json.dumps(message, separators=(",", ":")),
        }
    payload: dict[str, Any] = {
        "recipient": {"id": recipient_id},
        "message": message,
    }
    if channel is MetaSocialChannel.facebook_messenger:
        payload["messaging_type"] = "RESPONSE"
    return payload


def _profile_endpoint(config: Mapping[str, Any], channel: MetaSocialChannel) -> str:
    version = _graph_version(config)
    if channel is MetaSocialChannel.instagram_dm and _auth_mode(config) == (
        META_SOCIAL_AUTH_MODE_INDIVIDUAL
    ):
        return f"https://graph.instagram.com/{version}/me"
    return f"https://graph.facebook.com/{version}"


def _profile_fields(channel: MetaSocialChannel) -> str:
    if channel is MetaSocialChannel.facebook_messenger:
        return "first_name,last_name,name,profile_pic"
    return "username,name,profile_pic"


def _safe_profile(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    first = str(data.get("first_name") or "").strip()
    last = str(data.get("last_name") or "").strip()
    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    display_name = " ".join(part for part in (first, last) if part).strip()
    return {
        "display_name": display_name or name or username or None,
        "username": username or None,
        "profile_pic": str(data.get("profile_pic") or "").strip() or None,
    }


def _safe_receipt(response: httpx.Response) -> dict[str, Any]:
    receipt: dict[str, Any] = {"status_code": response.status_code}
    try:
        raw = response.json()
    except (ValueError, json.JSONDecodeError):
        return receipt
    if not isinstance(raw, dict):
        return receipt
    message_id = raw.get("message_id") or raw.get("id")
    recipient_id = raw.get("recipient_id")
    if message_id:
        receipt["provider_message_id"] = str(message_id)
    if recipient_id:
        receipt["provider_recipient_id"] = str(recipient_id)
    return receipt


class MetaSocialRuntimeRunner:
    """Transport-only runner for the two explicitly configured Meta accounts."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        errors: list[str] = []
        if str(config.get("provider") or "") != "meta_social":
            errors.append("provider_unsupported")
        for key in ("app_id", "facebook_page_id", "instagram_account_id"):
            if not str(config.get(key) or "").strip():
                errors.append(f"{key}_required")
        mode = _auth_mode(config)
        if mode != str(config.get("auth_mode") or mode):
            errors.append("auth_mode_invalid")
        credential_bindings = (
            (META_OAUTH_TOKEN_BINDING,)
            if mode == META_SOCIAL_AUTH_MODE_OAUTH
            else (FACEBOOK_TOKEN_BINDING, INSTAGRAM_TOKEN_BINDING)
        )
        for binding in credential_bindings + (
            WEBHOOK_SIGNING_SECRET_BINDING,
            WEBHOOK_VERIFY_TOKEN_BINDING,
        ):
            if not str(secret_material.get(binding) or "").strip():
                errors.append(f"{binding}_required")
        if errors:
            return ValidationResult(valid=False, error_codes=tuple(errors))

        timeout = float(config.get("timeout_seconds") or 10)
        probes = (
            (
                MetaSocialChannel.facebook_messenger,
                "https://graph.facebook.com",
            ),
            (
                MetaSocialChannel.instagram_dm,
                (
                    "https://graph.facebook.com"
                    if mode == META_SOCIAL_AUTH_MODE_OAUTH
                    else "https://graph.instagram.com"
                ),
            ),
        )
        for channel, host in probes:
            account_id = _configured_account_id(config, channel)
            url = (
                f"{host}/{_graph_version(config)}/me"
                if channel is MetaSocialChannel.instagram_dm
                and mode == META_SOCIAL_AUTH_MODE_INDIVIDUAL
                else f"{host}/{_graph_version(config)}/{account_id}"
            )
            try:
                response = httpx.get(
                    url,
                    params={"fields": "id"},
                    headers={
                        "Authorization": (
                            f"Bearer {secret_material[_credential_binding(config, channel)]}"
                        )
                    },
                    timeout=timeout,
                )
            except httpx.HTTPError:
                errors.append(f"{channel.value}_connection_unavailable")
                continue
            if response.status_code >= 400:
                errors.append(f"{channel.value}_credential_rejected")
                continue
            try:
                observed_id = str(response.json().get("id") or "").strip()
            except (AttributeError, ValueError, json.JSONDecodeError):
                observed_id = ""
            skip_account_compare = (
                channel is MetaSocialChannel.instagram_dm
                and mode == META_SOCIAL_AUTH_MODE_INDIVIDUAL
            )
            if observed_id and observed_id != account_id and not skip_account_compare:
                errors.append(f"{channel.value}_account_mismatch")
        return ValidationResult(valid=not errors, error_codes=tuple(errors))

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        if envelope.capability_id != META_SOCIAL_SEND_CAPABILITY:
            return self._rejected(envelope, "capability_unsupported")
        if str(envelope.payload.get("action") or "") == "fetch_profile":
            return self._fetch_profile(
                envelope, config=config, secret_material=secret_material
            )
        if str(envelope.payload.get("action") or "") != "send_direct_message":
            return self._rejected(envelope, "action_unsupported")
        params = envelope.payload.get("params")
        if not isinstance(params, dict):
            return self._rejected(envelope, "params_invalid")
        try:
            channel = MetaSocialChannel(str(params.get("channel") or ""))
        except ValueError:
            return self._rejected(envelope, "channel_unsupported")
        account_id = str(params.get("provider_account_id") or "").strip()
        recipient_id = str(params.get("recipient_id") or "").strip()
        body = str(params.get("body") or "").strip()
        if account_id != _configured_account_id(config, channel):
            return self._rejected(envelope, "provider_account_not_bound")
        if not recipient_id:
            return self._rejected(envelope, "recipient_required")
        if not body:
            return self._rejected(envelope, "body_required")
        payload = _payload(
            config=config, channel=channel, recipient_id=recipient_id, body=body
        )
        output: dict[str, Any] = {
            "channel": channel.value,
            "provider_account_id": account_id,
            "sent": False,
        }
        if bool(params.get("preview")):
            output["payload"] = payload
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                output=output,
            )
        credential = str(
            secret_material.get(_credential_binding(config, channel)) or ""
        ).strip()
        if not credential:
            return self._rejected(envelope, "channel_credential_missing")
        remaining = max(1.0, (envelope.deadline_at - datetime.now(UTC)).total_seconds())
        timeout = min(float(config.get("timeout_seconds") or 10), remaining)
        try:
            response = httpx.post(
                _endpoint(config, channel),
                json=payload,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
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

        receipt = _safe_receipt(response)
        output["status_code"] = response.status_code
        if response.status_code == 429 or response.status_code >= 500:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                output=output,
                external_receipt=receipt,
                error_code="provider_retryable_response",
            )
        if response.status_code >= 400:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                output=output,
                external_receipt=receipt,
                error_code="provider_rejected_message",
            )
        output["sent"] = True
        if not receipt.get("provider_message_id"):
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.reconciliation_required,
                output=output,
                external_receipt=receipt,
                error_code="provider_message_id_missing",
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output=output,
            external_receipt=receipt,
        )

    def _fetch_profile(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        params = envelope.payload.get("params")
        if not isinstance(params, dict):
            return self._rejected(envelope, "params_invalid")
        try:
            channel = MetaSocialChannel(str(params.get("channel") or ""))
        except ValueError:
            return self._rejected(envelope, "channel_unsupported")
        contact_id = str(params.get("contact_id") or "").strip()
        if not contact_id:
            return self._rejected(envelope, "contact_id_required")
        credential = str(
            secret_material.get(_credential_binding(config, channel)) or ""
        ).strip()
        if not credential:
            return self._rejected(envelope, "channel_credential_missing")
        timeout = min(
            float(config.get("timeout_seconds") or 10),
            max(1.0, (envelope.deadline_at - datetime.now(UTC)).total_seconds()),
        )
        try:
            if (
                channel is MetaSocialChannel.instagram_dm
                and _auth_mode(config) == META_SOCIAL_AUTH_MODE_INDIVIDUAL
            ):
                response = httpx.get(
                    _profile_endpoint(config, channel),
                    params={"fields": "id,username,name,profile_pic"},
                    headers={"Authorization": f"Bearer {credential}"},
                    timeout=timeout,
                )
            else:
                response = httpx.get(
                    f"{_profile_endpoint(config, channel)}/{contact_id}",
                    params={"fields": _profile_fields(channel)},
                    headers={"Authorization": f"Bearer {credential}"},
                    timeout=timeout,
                )
        except httpx.TimeoutException:
            return self._failed(envelope, OperationStatus.retryable, "profile_timeout")
        except httpx.RequestError:
            return self._failed(
                envelope, OperationStatus.retryable, "profile_unavailable"
            )
        if response.status_code >= 400:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="profile_rejected",
            )
        try:
            profile = _safe_profile(response.json())
        except (ValueError, json.JSONDecodeError):
            profile = _safe_profile({})
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output={"profile": profile},
        )

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
            status="healthy" if result.valid else "unavailable",
            details={"error_codes": list(result.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False

    @staticmethod
    def _rejected(envelope: OperationEnvelope, code: str) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.rejected,
            error_code=code,
        )

    @staticmethod
    def _failed(
        envelope: OperationEnvelope, status: OperationStatus, code: str
    ) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id,
            status=status,
            error_code=code,
        )
