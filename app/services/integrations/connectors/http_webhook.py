"""Bounded outbound HTTPS event-delivery connector."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
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

EVENT_DELIVERY_CAPABILITY = "events.deliver.v1"


def validate_https_url(value: Any) -> tuple[str | None, str | None]:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None, "https_url_required"
    if parsed.username or parsed.password:
        return None, "url_credentials_forbidden"
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return None, "private_egress_forbidden"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None, "private_egress_forbidden"
    return host, None


class HttpWebhookRunner:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        if manifest.capability(EVENT_DELIVERY_CAPABILITY) is None:
            return ValidationResult(valid=False, error_codes=("capability_missing",))
        _host, error = validate_https_url(config.get("url"))
        if error:
            return ValidationResult(valid=False, error_codes=(error,))
        method = str(config.get("method") or "POST").upper()
        if method not in {"POST", "PUT"}:
            return ValidationResult(valid=False, error_codes=("method_invalid",))
        try:
            response = self._request(
                "HEAD",
                str(config["url"]),
                headers={"User-Agent": "dotmac-sub-integration-validator/1"},
                timeout=float(config.get("timeout_seconds") or 10),
            )
        except httpx.HTTPError:
            return ValidationResult(valid=False, error_codes=("endpoint_unreachable",))
        return ValidationResult(
            valid=True,
            details={"response_status": response.status_code},
        )

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        if envelope.capability_id != EVENT_DELIVERY_CAPABILITY:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="capability_not_supported",
            )
        _host, error = validate_https_url(config.get("url"))
        if error:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code=error,
            )
        if envelope.payload.get("action") != "deliver_event":
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="operation_invalid",
            )
        params = envelope.payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        payload = params.get("payload")
        event_type = str(params.get("event_type") or "")
        if not isinstance(payload, dict) or not event_type:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="event_payload_invalid",
            )
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "dotmac-sub-integration/1",
            "X-Dotmac-Event": event_type,
            "X-Dotmac-Delivery-Id": str(envelope.operation_id),
            "Idempotency-Key": envelope.idempotency_key,
        }
        authorization = secret_material.get("authorization")
        if authorization:
            scheme = str(config.get("authorization_scheme") or "").strip()
            headers["Authorization"] = (
                f"{scheme} {authorization}" if scheme else authorization
            )
        signing_secret = secret_material.get("signing_secret")
        if signing_secret:
            signature = hmac.new(
                signing_secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Dotmac-Signature-256"] = f"sha256={signature}"
        try:
            response = self._request(
                str(config.get("method") or "POST").upper(),
                str(config["url"]),
                content=body,
                headers=headers,
                timeout=float(config.get("timeout_seconds") or 30),
            )
        except httpx.TimeoutException:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.reconciliation_required,
                error_code="delivery_outcome_ambiguous",
            )
        except httpx.RequestError:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                error_code="endpoint_unreachable",
            )
        receipt = {"response_status": response.status_code}
        if 200 <= response.status_code < 300:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.succeeded,
                external_receipt=receipt,
            )
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_seconds = int(retry_after) if retry_after else None
            except ValueError:
                retry_seconds = None
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                external_receipt=receipt,
                error_code="endpoint_retryable_response",
                retry_after_seconds=max(1, min(retry_seconds, 86400))
                if retry_seconds is not None
                else None,
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.rejected,
            external_receipt=receipt,
            error_code="endpoint_rejected",
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return self._client.request(method, url, **kwargs)
        return httpx.request(method, url, **kwargs)

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
            details={"error_codes": list(result.error_codes), **result.details},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False
