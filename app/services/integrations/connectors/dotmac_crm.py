"""Version-pinned DotMac CRM runtime with an explicit operation allow-list."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast
from uuid import UUID

from app.services.crm_client import CRMClient, CRMClientError
from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

CRM_SUBSCRIBER_OBSERVATION_CAPABILITY = "crm.subscriber_observation.v1"
CRM_TICKET_OBSERVATION_CAPABILITY = "crm.ticket_observation.v1"
CRM_OPERATIONAL_OBSERVATION_CAPABILITY = "crm.operational_observation.v1"
CRM_PORTAL_SESSION_CAPABILITY = "crm.portal_session.v1"
CRM_CHAT_SESSION_CAPABILITY = "crm.chat_session.v1"
CRM_QUOTE_COMMAND_CAPABILITY = "crm.quote_command.v1"
CRM_EVENT_RECEIVE_CAPABILITY = "crm.events.receive.v1"


class CrmTransport(Protocol):
    def resolve_subscriber_id(self, splynx_customer_id: int) -> str | None: ...
    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]: ...
    def list_subscribers(
        self,
        *,
        external_system: str | None = None,
        page: int = 1,
        per_page: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]: ...
    def list_tickets(
        self,
        subscriber_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "desc",
        use_cache: bool = True,
    ) -> list[dict[str, Any]]: ...
    def get_ticket(self, ticket_id: str) -> dict[str, Any]: ...
    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]: ...
    def create_portal_session(
        self,
        *,
        crm_subscriber_id: str,
        actor: str = "subscriber",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]: ...
    def create_widget_session(
        self,
        *,
        config_id: str,
        email: str,
        name: str | None,
        crm_subscriber_id: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]: ...
    def get_portal_referrals(self, crm_subscriber_id: str) -> dict[str, Any]: ...
    def get_portal_quotes(self, crm_subscriber_id: str) -> dict[str, Any]: ...
    def request_portal_quote(
        self,
        crm_subscriber_id: str,
        *,
        latitude: float,
        longitude: float,
        address: str | None = None,
        region: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]: ...
    def accept_portal_quote(
        self,
        crm_subscriber_id: str,
        quote_id: str,
        *,
        deposit_reference: str,
        deposit_amount: str,
        provider: str | None = None,
    ) -> dict[str, Any]: ...


# Ticket import legitimately needs subscriber identity observations to map a
# remote ticket to Sub's authoritative subscriber record.
_ACTIONS_BY_CAPABILITY = {
    CRM_SUBSCRIBER_OBSERVATION_CAPABILITY: {
        "resolve_subscriber_id",
        "get_subscriber",
        "list_subscribers",
    },
    CRM_TICKET_OBSERVATION_CAPABILITY: {
        "get_subscriber",
        "list_subscribers",
        "list_tickets",
        "get_ticket",
        "list_ticket_comments",
    },
    CRM_OPERATIONAL_OBSERVATION_CAPABILITY: {
        "get_portal_referrals",
        "get_portal_quotes",
    },
    CRM_PORTAL_SESSION_CAPABILITY: {"create_portal_session"},
    CRM_CHAT_SESSION_CAPABILITY: {"create_widget_session"},
    CRM_QUOTE_COMMAND_CAPABILITY: {
        "request_portal_quote",
        "accept_portal_quote",
    },
    CRM_EVENT_RECEIVE_CAPABILITY: set(),
}


class CrmTicketObservationSource(Protocol):
    """Narrow source contract consumed by the ticket domain resolver."""

    def list_subscribers(
        self,
        *,
        external_system: str | None = None,
        page: int = 1,
        per_page: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]: ...
    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]: ...
    def list_tickets(
        self,
        subscriber_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "desc",
        use_cache: bool = True,
    ) -> list[dict[str, Any]]: ...
    def get_ticket(self, ticket_id: str) -> dict[str, Any]: ...
    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]: ...


class DotmacCrmRunner:
    """Execute only declared CRM operations over the DB-free HTTP substrate."""

    def __init__(self, client_override: object | None = None) -> None:
        self._client_override = (
            cast(CrmTransport, client_override) if client_override is not None else None
        )

    def _client(
        self, config: Mapping[str, Any], secret_material: Mapping[str, str]
    ) -> CrmTransport:
        if self._client_override is not None:
            return self._client_override
        return CRMClient(
            base_url=str(config.get("base_url") or ""),
            service_token=secret_material.get("service_credentials") or "",
            timeout=float(config.get("timeout_seconds") or 45),
        )

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        if not str(config.get("base_url") or "").strip():
            return ValidationResult(valid=False, error_codes=("base_url_missing",))
        if self._client_override is None and not secret_material.get(
            "service_credentials"
        ):
            return ValidationResult(
                valid=False, error_codes=("service_credentials_missing",)
            )
        try:
            self._client(config, secret_material).list_tickets(
                limit=1,
                offset=0,
                order_by="updated_at",
                order_dir="desc",
                use_cache=False,
            )
        except CRMClientError:
            return ValidationResult(valid=False, error_codes=("crm_unreachable",))
        except Exception:
            return ValidationResult(valid=False, error_codes=("validation_failed",))
        return ValidationResult(valid=True)

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        allowed = _ACTIONS_BY_CAPABILITY.get(envelope.capability_id)
        if allowed is None:
            return self._result(
                envelope, OperationStatus.rejected, "capability_not_supported"
            )
        action = str(envelope.payload.get("action") or "")
        params = envelope.payload.get("params") or {}
        if not isinstance(params, dict) or action not in allowed:
            return self._result(
                envelope, OperationStatus.rejected, "operation_not_allowed"
            )
        try:
            output = self._execute_action(
                self._client(config, secret_material), action, params
            )
        except CRMClientError:
            return self._result(
                envelope, OperationStatus.retryable, "crm_transport_error"
            )
        except (TypeError, ValueError, KeyError):
            return self._result(envelope, OperationStatus.rejected, "operation_invalid")
        except Exception:
            return self._result(envelope, OperationStatus.failed, "connector_failed")
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output=output,
        )

    @staticmethod
    def _result(
        envelope: OperationEnvelope, status: OperationStatus, error: str
    ) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id,
            status=status,
            error_code=error,
        )

    def _execute_action(
        self, client: CrmTransport, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if action == "resolve_subscriber_id":
            return {
                "value": client.resolve_subscriber_id(int(params["splynx_customer_id"]))
            }
        if action == "list_subscribers":
            return {
                "items": client.list_subscribers(
                    external_system=params.get("external_system"),
                    page=int(params.get("page") or 1),
                    per_page=int(params.get("per_page") or 100),
                    use_cache=False,
                )
            }
        if action == "get_subscriber":
            return {"item": client.get_subscriber(str(params["subscriber_id"]))}
        if action == "list_tickets":
            return {
                "items": client.list_tickets(
                    subscriber_id=params.get("subscriber_id"),
                    limit=int(params.get("limit") or 100),
                    offset=int(params.get("offset") or 0),
                    order_by=str(params.get("order_by") or "created_at"),
                    order_dir=str(params.get("order_dir") or "desc"),
                    use_cache=False,
                )
            }
        if action == "get_ticket":
            return {"item": client.get_ticket(str(params["ticket_id"]))}
        if action == "list_ticket_comments":
            return {
                "items": client.list_ticket_comments(
                    str(params["ticket_id"]), use_cache=False
                )
            }
        if action == "create_portal_session":
            return {
                "item": client.create_portal_session(
                    crm_subscriber_id=str(params["crm_subscriber_id"]),
                    actor=str(params.get("actor") or "subscriber"),
                    scopes=list(params.get("scopes") or []),
                )
            }
        if action == "create_widget_session":
            return {
                "item": client.create_widget_session(
                    config_id=str(params["config_id"]),
                    email=str(params["email"]),
                    name=str(params["name"]) if params.get("name") else None,
                    crm_subscriber_id=(
                        str(params["crm_subscriber_id"])
                        if params.get("crm_subscriber_id")
                        else None
                    ),
                    metadata=dict(params.get("metadata") or {}),
                )
            }
        if action == "get_portal_referrals":
            return {
                "item": client.get_portal_referrals(str(params["crm_subscriber_id"]))
            }
        if action == "get_portal_quotes":
            return {"item": client.get_portal_quotes(str(params["crm_subscriber_id"]))}
        if action == "request_portal_quote":
            return {
                "item": client.request_portal_quote(
                    str(params["crm_subscriber_id"]),
                    latitude=float(params["latitude"]),
                    longitude=float(params["longitude"]),
                    address=params.get("address"),
                    region=params.get("region"),
                    note=params.get("note"),
                )
            }
        if action == "accept_portal_quote":
            return {
                "item": client.accept_portal_quote(
                    str(params["crm_subscriber_id"]),
                    str(params["quote_id"]),
                    deposit_reference=str(params["deposit_reference"]),
                    deposit_amount=str(params["deposit_amount"]),
                    provider=params.get("provider"),
                )
            }
        raise ValueError("unsupported CRM action")

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        validation = self.validate(
            manifest=manifest, config=config, secret_material=secret_material
        )
        return HealthResult(
            status="healthy" if validation.valid else "unavailable",
            details={"error_codes": list(validation.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False


class RuntimeCrmObservationSource:
    """Ticket-source facade pinned to one integration-run binding."""

    def __init__(self, execute_operation) -> None:
        self._execute_operation = execute_operation

    def _execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        result = self._execute_operation(action, params)
        if result.status != OperationStatus.succeeded:
            raise CRMClientError(result.error_code or "CRM capability failed")
        return dict(result.output)

    def list_subscribers(
        self,
        *,
        external_system: str | None = None,
        page: int = 1,
        per_page: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        return list(
            self._execute(
                "list_subscribers",
                {
                    "external_system": external_system,
                    "page": page,
                    "per_page": per_page,
                },
            ).get("items")
            or []
        )

    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        return dict(
            self._execute("get_subscriber", {"subscriber_id": subscriber_id}).get(
                "item"
            )
            or {}
        )

    def list_tickets(
        self,
        subscriber_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "desc",
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        return list(
            self._execute(
                "list_tickets",
                {
                    "subscriber_id": subscriber_id,
                    "limit": limit,
                    "offset": offset,
                    "order_by": order_by,
                    "order_dir": order_dir,
                },
            ).get("items")
            or []
        )

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict(
            self._execute("get_ticket", {"ticket_id": ticket_id}).get("item") or {}
        )

    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        return list(
            self._execute("list_ticket_comments", {"ticket_id": ticket_id}).get("items")
            or []
        )
