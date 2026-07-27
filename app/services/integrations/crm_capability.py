"""Sub-side facade for every DotMac CRM transport operation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.services.crm_client import CRMClientError
from app.services.integrations import installations
from app.services.integrations.connectors.dotmac_crm import (
    CRM_CHAT_SESSION_CAPABILITY,
    CRM_EVENT_RECEIVE_CAPABILITY,
    CRM_OPERATIONAL_OBSERVATION_CAPABILITY,
    CRM_PORTAL_SESSION_CAPABILITY,
    CRM_QUOTE_COMMAND_CAPABILITY,
    CRM_SUBSCRIBER_OBSERVATION_CAPABILITY,
    CRM_TICKET_OBSERVATION_CAPABILITY,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)
from app.services.secrets import resolve_secret

CONNECTOR_KEY = "dotmac.crm"
_OPERATION_OBSERVATION_KEY = "integration.crm.capability.crm.operational_observation.v1"


class CrmCapabilityClient:
    """Client-shaped facade whose methods resolve an enabled typed binding."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def _execute(
        self,
        capability_id: str,
        action: str,
        params: dict[str, Any],
        *,
        trigger: OperationTrigger,
        correlation_id: str,
    ) -> dict[str, Any]:
        binding = installations.require_enabled_capability_binding(
            self._db,
            connector_key=CONNECTOR_KEY,
            capability_id=capability_id,
        )
        context = build_execution_context(self._db, capability_binding_id=binding.id)
        result = make_operation_executor(
            context,
            correlation_id=correlation_id[:160],
            trigger=trigger,
            actor="integration.crm",
        )(action, params)
        if result.status != OperationStatus.succeeded:
            raise CRMClientError(result.error_code or "CRM capability failed")
        return dict(result.output)

    def resolve_subscriber_id(self, splynx_customer_id: int) -> str | None:
        value = self._execute(
            CRM_SUBSCRIBER_OBSERVATION_CAPABILITY,
            "resolve_subscriber_id",
            {"splynx_customer_id": splynx_customer_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"crm-subscriber:splynx:{splynx_customer_id}",
        ).get("value")
        return str(value) if value else None

    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_SUBSCRIBER_OBSERVATION_CAPABILITY,
                "get_subscriber",
                {"subscriber_id": subscriber_id},
                trigger=OperationTrigger.reconcile,
                correlation_id=f"crm-subscriber:{subscriber_id}",
            ).get("item")
            or {}
        )

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
                CRM_SUBSCRIBER_OBSERVATION_CAPABILITY,
                "list_subscribers",
                {
                    "external_system": external_system,
                    "page": page,
                    "per_page": per_page,
                },
                trigger=OperationTrigger.reconcile,
                correlation_id=f"crm-subscribers:{page}:{per_page}",
            ).get("items")
            or []
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
                CRM_TICKET_OBSERVATION_CAPABILITY,
                "list_tickets",
                {
                    "subscriber_id": subscriber_id,
                    "limit": limit,
                    "offset": offset,
                    "order_by": order_by,
                    "order_dir": order_dir,
                },
                trigger=OperationTrigger.reconcile,
                correlation_id=f"crm-tickets:{subscriber_id or 'all'}:{offset}",
            ).get("items")
            or []
        )

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_TICKET_OBSERVATION_CAPABILITY,
                "get_ticket",
                {"ticket_id": ticket_id},
                trigger=OperationTrigger.reconcile,
                correlation_id=f"crm-ticket:{ticket_id}",
            ).get("item")
            or {}
        )

    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        return list(
            self._execute(
                CRM_TICKET_OBSERVATION_CAPABILITY,
                "list_ticket_comments",
                {"ticket_id": ticket_id},
                trigger=OperationTrigger.reconcile,
                correlation_id=f"crm-ticket-comments:{ticket_id}",
            ).get("items")
            or []
        )

    def _operational(
        self, action: str, params: dict[str, Any], correlation: str
    ) -> dict[str, Any]:
        from app.services import job_heartbeat

        try:
            result = self._execute(
                CRM_OPERATIONAL_OBSERVATION_CAPABILITY,
                action,
                params,
                trigger=OperationTrigger.reconcile,
                correlation_id=correlation,
            )
        except Exception as exc:
            job_heartbeat.record_result(
                _OPERATION_OBSERVATION_KEY,
                status="failed",
                detail={
                    "action": action,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        job_heartbeat.record_success(_OPERATION_OBSERVATION_KEY)
        job_heartbeat.record_result(
            _OPERATION_OBSERVATION_KEY,
            status="success",
            detail={"action": action},
        )
        return result

    def get_portal_referrals(self, crm_subscriber_id: str) -> dict[str, Any]:
        return dict(
            self._operational(
                "get_portal_referrals",
                {"crm_subscriber_id": crm_subscriber_id},
                f"crm-portal-referrals:{crm_subscriber_id}",
            ).get("item")
            or {}
        )

    def get_portal_quotes(self, crm_subscriber_id: str) -> dict[str, Any]:
        return dict(
            self._operational(
                "get_portal_quotes",
                {"crm_subscriber_id": crm_subscriber_id},
                f"crm-portal-quotes:{crm_subscriber_id}",
            ).get("item")
            or {}
        )

    def create_portal_session(
        self,
        *,
        crm_subscriber_id: str,
        actor: str = "subscriber",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_PORTAL_SESSION_CAPABILITY,
                "create_portal_session",
                {
                    "crm_subscriber_id": crm_subscriber_id,
                    "actor": actor,
                    "scopes": list(scopes or []),
                },
                trigger=OperationTrigger.interactive,
                correlation_id=f"crm-portal-session:{actor}:{crm_subscriber_id}",
            ).get("item")
            or {}
        )

    def create_widget_session(
        self,
        *,
        config_id: str,
        email: str,
        name: str | None,
        crm_subscriber_id: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_CHAT_SESSION_CAPABILITY,
                "create_widget_session",
                {
                    "config_id": config_id,
                    "email": email,
                    "name": name,
                    "crm_subscriber_id": crm_subscriber_id,
                    "metadata": metadata,
                },
                trigger=OperationTrigger.interactive,
                correlation_id=(
                    f"crm-chat-session:{metadata.get('surface', 'portal')}:"
                    f"{metadata.get('subscriber_id') or metadata.get('reseller_id') or 'principal'}"
                ),
            ).get("item")
            or {}
        )

    def request_portal_quote(
        self,
        crm_subscriber_id: str,
        *,
        latitude: float,
        longitude: float,
        address: str | None = None,
        region: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_QUOTE_COMMAND_CAPABILITY,
                "request_portal_quote",
                {
                    "crm_subscriber_id": crm_subscriber_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "address": address,
                    "region": region,
                    "note": note,
                },
                trigger=OperationTrigger.interactive,
                correlation_id=f"crm-quote-request:{crm_subscriber_id}:{latitude}:{longitude}",
            ).get("item")
            or {}
        )

    def accept_portal_quote(
        self,
        crm_subscriber_id: str,
        quote_id: str,
        *,
        deposit_reference: str,
        deposit_amount: str,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._execute(
                CRM_QUOTE_COMMAND_CAPABILITY,
                "accept_portal_quote",
                {
                    "crm_subscriber_id": crm_subscriber_id,
                    "quote_id": quote_id,
                    "deposit_reference": deposit_reference,
                    "deposit_amount": deposit_amount,
                    "provider": provider,
                },
                trigger=OperationTrigger.interactive,
                correlation_id=f"crm-quote-accept:{quote_id}:{deposit_reference}",
            ).get("item")
            or {}
        )


def capability_client(db: Session) -> CrmCapabilityClient:
    return CrmCapabilityClient(db)


def active_config(db: Session, capability_id: str) -> dict[str, Any]:
    binding = installations.require_enabled_capability_binding(
        db, connector_key=CONNECTOR_KEY, capability_id=capability_id
    )
    revision = binding.installation.current_config_revision
    if revision is None:
        raise installations.InstallationError("CRM configuration revision missing")
    return dict(revision.config_json or {})


def inbound_secret_material(
    db: Session, *, secret_resolver: Callable[[str | None], str | None] = resolve_secret
) -> tuple[Any, dict[str, str]]:
    binding = installations.require_enabled_capability_binding(
        db, connector_key=CONNECTOR_KEY, capability_id=CRM_EVENT_RECEIVE_CAPABILITY
    )
    context = build_execution_context(
        db, capability_binding_id=binding.id, secret_resolver=secret_resolver
    )
    return binding, dict(context.secret_material)
