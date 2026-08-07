"""Database-free Nextcloud Talk transport for the integration runtime."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

NEXTCLOUD_TALK_CAPABILITY = "collaboration.message.send.v1"
_CONVERSATION_API = "/ocs/v2.php/apps/spreed/api/v4"
_CHAT_API = "/ocs/v2.php/apps/spreed/api/v1"


class TalkTransportError(RuntimeError):
    """Sanitized provider failure with retry/reconciliation classification."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.ambiguous = ambiguous


def normalize_base_url(value: object) -> str:
    """Return a safe public HTTPS Nextcloud origin, including a sub-path."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url_invalid") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("base_url_https_required")
    if not parsed.hostname:
        raise ValueError("base_url_host_required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url_credentials_forbidden")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url_query_or_fragment_forbidden")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("base_url_local_host_forbidden")
    _validate_public_host(hostname, parsed.port or 443)
    netloc = hostname if parsed.port in (None, 443) else f"{hostname}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, "", ""))


def _validate_public_host(hostname: str, port: int) -> None:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    if literal is not None:
        addresses.add(literal)
    else:
        try:
            for item in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            ):
                addresses.add(ipaddress.ip_address(item[4][0]))
        except (OSError, ValueError) as exc:
            raise ValueError("base_url_dns_unavailable") from exc
    if not addresses:
        raise ValueError("base_url_dns_unavailable")
    if any(not address.is_global for address in addresses):
        raise ValueError("base_url_unsafe_address")


def _timeout(
    config: Mapping[str, Any], envelope: OperationEnvelope | None = None
) -> float:
    configured = float(config.get("timeout_seconds") or 30)
    bounded = min(max(configured, 1.0), 120.0)
    if envelope is None:
        return bounded
    remaining = max(1.0, (envelope.deadline_at - datetime.now(UTC)).total_seconds())
    return min(bounded, remaining)


def _success_status(value: object) -> bool:
    if value in (100, "100"):
        return True
    try:
        numeric = int(str(value))
    except (TypeError, ValueError):
        return False
    return 200 <= numeric < 300


def _ocs_data(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TalkTransportError(
            "provider_response_invalid",
            status_code=response.status_code,
            retryable=response.status_code >= 500,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ocs"), dict):
        raise TalkTransportError(
            "provider_response_invalid",
            status_code=response.status_code,
        )
    ocs = payload["ocs"]
    meta = ocs.get("meta") if isinstance(ocs.get("meta"), dict) else {}
    status_code = meta.get("statuscode", response.status_code)
    if not _success_status(status_code):
        try:
            numeric = int(str(status_code))
        except (TypeError, ValueError):
            numeric = response.status_code
        code = "provider_rejected"
        if numeric == 401:
            code = "provider_authentication_failed"
        elif numeric == 403:
            code = "room_forbidden"
        elif numeric == 404:
            code = "provider_resource_not_found"
        elif numeric == 429:
            code = "provider_rate_limited"
        raise TalkTransportError(
            code,
            status_code=numeric,
            retryable=numeric == 429 or numeric >= 500,
        )
    return ocs.get("data")


def _request(
    *,
    config: Mapping[str, Any],
    secret_material: Mapping[str, str],
    method: str,
    api_base: str,
    path: str,
    envelope: OperationEnvelope | None = None,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    send_json: bool = False,
    ambiguous_on_timeout: bool = False,
) -> tuple[Any, int]:
    base_url = normalize_base_url(config.get("url"))
    username = str(config.get("notifier_username") or "").strip()
    password = str(secret_material.get("app_password") or "").strip()
    if not username:
        raise TalkTransportError("notifier_username_required")
    if not password:
        raise TalkTransportError("app_password_required")
    query = dict(params or {})
    query.setdefault("format", "json")
    try:
        with httpx.Client(
            timeout=_timeout(config, envelope),
            follow_redirects=False,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            auth=httpx.BasicAuth(username, password),
        ) as client:
            response = client.request(
                method,
                f"{base_url}{api_base}{path}",
                params=query,
                json=dict(data) if send_json and data is not None else None,
                data=None if send_json or data is None else dict(data),
            )
    except httpx.ConnectTimeout as exc:
        raise TalkTransportError("provider_connect_timeout", retryable=True) from exc
    except httpx.TimeoutException as exc:
        raise TalkTransportError(
            "provider_outcome_ambiguous"
            if ambiguous_on_timeout
            else "provider_timeout",
            retryable=not ambiguous_on_timeout,
            ambiguous=ambiguous_on_timeout,
        ) from exc
    except httpx.RequestError as exc:
        raise TalkTransportError("provider_unavailable", retryable=True) from exc
    if 300 <= response.status_code < 400:
        raise TalkTransportError(
            "provider_redirect_rejected",
            status_code=response.status_code,
        )
    return _ocs_data(response), response.status_code


def _room_token(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("token", "roomToken"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _message_id(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("id")
    return str(value) if value is not None else None


class NextcloudTalkRuntimeRunner:
    """Transport-only Talk runner; it receives no Selfcare database handle."""

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        errors: list[str] = []
        try:
            normalize_base_url(config.get("url"))
        except ValueError as exc:
            errors.append(str(exc))
        if not str(config.get("notifier_username") or "").strip():
            errors.append("notifier_username_required")
        if not str(secret_material.get("app_password") or "").strip():
            errors.append("app_password_required")
        timeout = config.get("timeout_seconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            errors.append("timeout_seconds_invalid")
        elif not 1 <= float(timeout) <= 120:
            errors.append("timeout_seconds_out_of_range")
        if errors:
            return ValidationResult(valid=False, error_codes=tuple(errors))
        try:
            _request(
                config=config,
                secret_material=secret_material,
                method="GET",
                api_base=_CONVERSATION_API,
                path="/room",
                params={"limit": 1},
            )
        except TalkTransportError as exc:
            return ValidationResult(valid=False, error_codes=(exc.code,))
        return ValidationResult(valid=True)

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        if envelope.capability_id != NEXTCLOUD_TALK_CAPABILITY:
            return self._failed(
                envelope, OperationStatus.rejected, "capability_unsupported"
            )
        action = str(envelope.payload.get("action") or "")
        params = envelope.payload.get("params")
        if not isinstance(params, dict):
            return self._failed(envelope, OperationStatus.rejected, "params_invalid")
        try:
            if action == "create_direct_room":
                return self._create_direct_room(
                    envelope,
                    config=config,
                    secret_material=secret_material,
                    params=params,
                )
            if action == "post_message":
                return self._post_message(
                    envelope,
                    config=config,
                    secret_material=secret_material,
                    params=params,
                )
            if action == "find_message":
                return self._find_message(
                    envelope,
                    config=config,
                    secret_material=secret_material,
                    params=params,
                )
            return self._failed(
                envelope, OperationStatus.rejected, "action_unsupported"
            )
        except TalkTransportError as exc:
            if exc.ambiguous:
                status = OperationStatus.reconciliation_required
            elif exc.retryable:
                status = OperationStatus.retryable
            else:
                status = OperationStatus.rejected
            return self._failed(
                envelope,
                status,
                exc.code,
                status_code=exc.status_code,
            )

    def _create_direct_room(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
        params: Mapping[str, Any],
    ) -> OperationResult:
        invite = str(params.get("invite") or "").strip()
        if not invite:
            return self._failed(envelope, OperationStatus.rejected, "invite_required")
        data, status_code = _request(
            config=config,
            secret_material=secret_material,
            method="POST",
            api_base=_CONVERSATION_API,
            path="/room",
            envelope=envelope,
            data={"roomType": 1, "invite": invite},
            send_json=True,
        )
        token = _room_token(data)
        if not token:
            return self._failed(
                envelope,
                OperationStatus.rejected,
                "room_token_missing",
                status_code=status_code,
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output={"room_token": token},
            external_receipt={"status_code": status_code},
        )

    def _post_message(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
        params: Mapping[str, Any],
    ) -> OperationResult:
        token = str(params.get("room_token") or "").strip()
        message = str(params.get("message") or "").strip()
        reference_id = str(params.get("reference_id") or "").strip()
        if not token:
            return self._failed(
                envelope, OperationStatus.rejected, "room_token_required"
            )
        if not message:
            return self._failed(envelope, OperationStatus.rejected, "message_required")
        if len(reference_id) != 64:
            return self._failed(
                envelope, OperationStatus.rejected, "reference_id_invalid"
            )
        data, status_code = _request(
            config=config,
            secret_material=secret_material,
            method="POST",
            api_base=_CHAT_API,
            path=f"/chat/{token}",
            envelope=envelope,
            data={"message": message, "referenceId": reference_id},
            ambiguous_on_timeout=True,
        )
        message_id = _message_id(data)
        receipt: dict[str, Any] = {
            "status_code": status_code,
            "reference_id": reference_id,
        }
        if message_id:
            receipt["provider_message_id"] = message_id
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output={"message_id": message_id, "reference_id": reference_id},
            external_receipt=receipt,
        )

    def _find_message(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
        params: Mapping[str, Any],
    ) -> OperationResult:
        token = str(params.get("room_token") or "").strip()
        reference_id = str(params.get("reference_id") or "").strip()
        if not token:
            return self._failed(
                envelope, OperationStatus.rejected, "room_token_required"
            )
        if len(reference_id) != 64:
            return self._failed(
                envelope, OperationStatus.rejected, "reference_id_invalid"
            )
        data, status_code = _request(
            config=config,
            secret_material=secret_material,
            method="GET",
            api_base=_CHAT_API,
            path=f"/chat/{token}",
            envelope=envelope,
            params={
                "lookIntoFuture": 0,
                "limit": 10,
                "referenceId": reference_id,
            },
        )
        rows = (
            data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        )
        matched = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("referenceId") or "") == reference_id
            ),
            None,
        )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output={
                "found": matched is not None,
                "message_id": _message_id(matched),
                "reference_id": reference_id,
            },
            external_receipt={"status_code": status_code},
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
    def _failed(
        envelope: OperationEnvelope,
        status: OperationStatus,
        code: str,
        *,
        status_code: int | None = None,
    ) -> OperationResult:
        receipt = {"status_code": status_code} if status_code is not None else {}
        return OperationResult(
            operation_id=envelope.operation_id,
            status=status,
            output={},
            external_receipt=receipt,
            error_code=code[:120],
        )
