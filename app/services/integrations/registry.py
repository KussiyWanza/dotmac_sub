"""Explicit connector definition registry and compatibility projections."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass

from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    DataAccessManifest,
    EgressManifest,
    HealthManifest,
    RuntimeManifest,
    SecretBindingManifest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectorRegistryEntry:
    """Compatibility projection consumed by the current marketplace UI."""

    key: str
    name: str
    version: str
    connector_type: str
    description: str
    module_name: str
    file_size_bytes: int


def _dotmac_erp_manifest(
    *,
    version: str,
    include_workforce_attendance: bool,
    include_material_webhook: bool = False,
) -> ConnectorManifest:
    capabilities = [
        CapabilityManifest(
            id="erp.outbox.deliver.v1",
            modes=(CapabilityMode.scheduled, CapabilityMode.event),
        ),
        CapabilityManifest(
            id="erp.status.read.v1",
            modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
        ),
        CapabilityManifest(
            id="erp.inventory.read.v1",
            modes=(CapabilityMode.interactive, CapabilityMode.manual),
        ),
        CapabilityManifest(
            id="erp.operational_context.sync.v1",
            modes=(CapabilityMode.scheduled,),
        ),
        CapabilityManifest(
            id="erp.regulatory.read.v1",
            modes=(CapabilityMode.interactive, CapabilityMode.manual),
        ),
    ]
    if include_workforce_attendance:
        capabilities.extend(
            (
                CapabilityManifest(
                    id="workforce.attendance.read.v1",
                    modes=(CapabilityMode.interactive,),
                ),
                CapabilityManifest(
                    id="workforce.attendance.punch.v1",
                    modes=(CapabilityMode.interactive,),
                ),
            )
        )
    if include_material_webhook:
        capabilities.append(
            CapabilityManifest(
                id="erp.material_status.webhook.v1",
                modes=(CapabilityMode.inbound,),
            )
        )
    properties: dict[str, object] = {
        "base_url": {"type": "string"},
        "timeout_seconds": {"type": "integer"},
        "max_retries": {"type": "integer"},
    }
    if include_workforce_attendance:
        properties.update(
            {
                "interactive_timeout_seconds": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 15,
                },
                "interactive_max_retries": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 0,
                    "maximum": 2,
                },
            }
        )
    reads = [
        "field.erp_outbox",
        "operations.context_projection",
        "inventory.query",
        "regulatory.query",
    ]
    classifications = ["financial", "operations", "inventory"]
    if include_workforce_attendance:
        reads.extend(("workforce.attendance_state", "workforce.browser_location"))
        classifications.extend(("workforce", "location"))
    return ConnectorManifest(
        key="dotmac.erp",
        name="DotMac ERP",
        version=version,
        connector_type="erp",
        description="First-party ERP transport and observation connector.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.dotmac_erp",
        ),
        capabilities=tuple(capabilities),
        config_schema={
            "type": "object",
            "properties": properties,
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="service_credentials"),
            *(
                (SecretBindingManifest(name="webhook_signing_secret"),)
                if include_material_webhook
                else ()
            ),
        ),
        data_access=DataAccessManifest(
            reads=tuple(reads),
            emits=("erp.transport_observation",),
            classifications=tuple(classifications),
        ),
        egress=EgressManifest(hosts=("erp.dotmac.io",)),
        health=HealthManifest(operation="connection.validate.v1"),
    )


def _paystack_manifest(
    *,
    version: str,
    include_safe_defaults: bool,
    require_public_key: bool,
) -> ConnectorManifest:
    """Build one immutable Paystack manifest retained by exact version/digest.

    Paystack 1.0.0 was changed in place by the payment control-plane cutover.
    Both deployed 1.0.0 digests remain executable during the bounded adoption
    window; 1.0.1 is the first correctly versioned definition.
    """

    base_url: dict[str, object] = {
        "type": "string",
        **({"default": "https://api.paystack.co"} if include_safe_defaults else {}),
    }
    timeout_seconds: dict[str, object] = {
        "type": "integer",
        **({"default": 30} if include_safe_defaults else {}),
    }
    default_currency: dict[str, object] = {
        "type": "string",
        **({"default": "NGN"} if include_safe_defaults else {}),
    }
    return ConnectorManifest(
        key="paystack",
        name="Paystack",
        version=version,
        connector_type="payment",
        description="Online payment gateway integration.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.payment_gateway",
        ),
        capabilities=(
            CapabilityManifest(
                id="payments.intent.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="payments.webhook.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="payments.reconcile.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
            ),
            CapabilityManifest(
                id="payments.refund.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": base_url,
                "timeout_seconds": timeout_seconds,
                "default_currency": default_currency,
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="gateway_credentials"),
            SecretBindingManifest(name="public_key", required=require_public_key),
        ),
        data_access=DataAccessManifest(
            reads=("financial.payment_intent",),
            emits=("financial.payment_provider_observation",),
            classifications=("financial", "customer_contact"),
        ),
        egress=EgressManifest(hosts=("api.paystack.co",)),
        health=HealthManifest(operation="connection.validate.v1"),
    )


def _dotmac_crm_manifest(
    *,
    version: str,
    include_chat_session: bool,
) -> ConnectorManifest:
    """Build the current CRM manifest and its bounded pre-chat predecessor."""

    capabilities = [
        CapabilityManifest(
            id="crm.subscriber_observation.v1",
            modes=(
                CapabilityMode.scheduled,
                CapabilityMode.manual,
                CapabilityMode.reconcile,
            ),
        ),
        CapabilityManifest(
            id="crm.ticket_observation.v1",
            modes=(
                CapabilityMode.scheduled,
                CapabilityMode.manual,
                CapabilityMode.reconcile,
            ),
        ),
        CapabilityManifest(
            id="crm.operational_observation.v1",
            modes=(
                CapabilityMode.scheduled,
                CapabilityMode.interactive,
                CapabilityMode.reconcile,
            ),
        ),
        CapabilityManifest(
            id="crm.portal_session.v1",
            modes=(CapabilityMode.interactive,),
        ),
    ]
    if include_chat_session:
        capabilities.append(
            CapabilityManifest(
                id="crm.chat_session.v1",
                modes=(CapabilityMode.interactive,),
            )
        )
    capabilities.extend(
        (
            CapabilityManifest(
                id="crm.quote_command.v1",
                modes=(CapabilityMode.interactive,),
            ),
            CapabilityManifest(
                id="crm.events.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
        )
    )
    properties: dict[str, dict[str, object]] = {
        "base_url": {"type": "string"},
        "timeout_seconds": {"type": "number"},
        "public_portal_api_base": {"type": "string"},
    }
    if include_chat_session:
        properties.update(
            {
                "chat_widget_config_id": {"type": "string"},
                "chat_ws_url": {"type": "string"},
            }
        )
    return ConnectorManifest(
        key="dotmac.crm",
        name="DotMac CRM",
        version=version,
        connector_type="crm",
        description="First-party CRM observations, commands, sessions, and inbound events.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.dotmac_crm",
        ),
        capabilities=tuple(capabilities),
        config_schema={
            "type": "object",
            "properties": properties,
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="service_credentials"),
            SecretBindingManifest(name="webhook_signing_secret", required=False),
        ),
        data_access=DataAccessManifest(
            reads=(
                "subscriber.external_identity",
                "portal.command_request",
                *(("portal.chat_session_request",) if include_chat_session else ()),
            ),
            emits=(
                "crm.external_observation",
                "crm.inbound_event_observation",
                *(("crm.chat_session",) if include_chat_session else ()),
            ),
            classifications=("customer_contact", "support_content", "operations"),
        ),
        egress=EgressManifest(hosts=("crm.dotmac.io",)),
        health=HealthManifest(operation="connection.validate.v1"),
    )


def _meta_social_manifest(
    *,
    version: str,
    include_shared_oauth: bool,
    include_auth_mode: bool,
) -> ConnectorManifest:
    """Build immutable Meta Social manifests for exact version/digest pins."""
    properties: dict[str, dict[str, object]] = {
        "provider": {"type": "string", "enum": ["meta_social"]},
        "app_id": {"type": "string"},
        "facebook_page_id": {"type": "string"},
        "facebook_auth_mode": {
            "type": "string",
            "enum": (
                ["meta_oauth", "page_access_token"]
                if include_shared_oauth
                else ["page_access_token"]
            ),
        },
        "instagram_account_id": {"type": "string"},
        "instagram_auth_mode": {
            "type": "string",
            "enum": (
                ["meta_oauth", "instagram_login"]
                if include_shared_oauth
                else ["instagram_login"]
            ),
        },
        "webhook_url": {"type": "string"},
        "graph_version": {"type": "string"},
        "timeout_seconds": {"type": "integer"},
    }
    required = [
        "provider",
        "app_id",
        "facebook_page_id",
        "facebook_auth_mode",
        "instagram_account_id",
        "instagram_auth_mode",
        "graph_version",
    ]
    secrets = [
        SecretBindingManifest(
            name="facebook_page_access_token", required=not include_shared_oauth
        ),
        SecretBindingManifest(
            name="instagram_login_access_token", required=not include_shared_oauth
        ),
        SecretBindingManifest(name="webhook_signing_secret"),
        SecretBindingManifest(name="webhook_verify_token"),
    ]
    if include_auth_mode:
        properties["auth_mode"] = {
            "type": "string",
            "enum": ["oauth", "individual"],
        }
        required.insert(2, "auth_mode")
    if include_shared_oauth:
        secrets.insert(
            0, SecretBindingManifest(name="meta_oauth_access_token", required=False)
        )
    return ConnectorManifest(
        key="meta.social",
        name="Meta Social Inbox",
        version=version,
        connector_type="messaging",
        description=(
            "Facebook Page Messenger and Instagram Login transport for Team Inbox."
        ),
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.meta_social_runtime",
        ),
        capabilities=(
            CapabilityManifest(
                id="messaging.send.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="messaging.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        secrets=tuple(secrets),
        data_access=DataAccessManifest(
            reads=("communications.outbound_message",),
            emits=("communications.inbound_message_observation",),
            classifications=("customer_contact", "message_content"),
        ),
        egress=EgressManifest(hosts=("graph.facebook.com", "graph.instagram.com")),
        health=HealthManifest(operation="connection.validate.v1"),
    )


_DEFINITIONS: tuple[ConnectorManifest, ...] = (
    ConnectorManifest(
        key="fiber.inquiry.http",
        name="Fiber Website Inquiry",
        version="1.0.0",
        connector_type="messaging",
        description="Signed fiber.dotmac.ng inquiry ingress for Team Inbox.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.fiber_inquiry_http",
        ),
        capabilities=(
            CapabilityManifest(
                id="communications.fiber_inquiry.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "signature_header": {"type": "string", "minLength": 1},
                "delivery_id_header": {"type": "string", "minLength": 1},
                "signature_prefix": {"type": "string"},
                "site_id": {"type": "string", "minLength": 1},
            },
            "required": [
                "signature_header",
                "delivery_id_header",
                "signature_prefix",
                "site_id",
            ],
            "additionalProperties": False,
        },
        secrets=(SecretBindingManifest(name="webhook_signing_secret"),),
        data_access=DataAccessManifest(
            emits=("communications.inbound_message_observation",),
            classifications=("customer_contact", "message_content"),
        ),
        egress=EgressManifest(),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="lead.capture.http",
        name="Lead Capture Webhook",
        version="1.0.0",
        connector_type="sales",
        description=(
            "Provider-neutral signed ingress for canonical lead-capture payloads."
        ),
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.lead_capture_http",
        ),
        capabilities=(
            CapabilityManifest(
                id="sales.lead_capture.v1",
                modes=(CapabilityMode.inbound,),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "signature_header": {"type": "string", "minLength": 1},
                "delivery_id_header": {"type": "string", "minLength": 1},
                "signature_prefix": {"type": "string"},
            },
            "required": [
                "signature_header",
                "delivery_id_header",
                "signature_prefix",
            ],
            "additionalProperties": False,
        },
        secrets=(SecretBindingManifest(name="webhook_signing_secret"),),
        data_access=DataAccessManifest(
            emits=("sales.lead_capture_observation",),
            classifications=("customer_contact", "marketing_attribution"),
        ),
        egress=EgressManifest(),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="webhook.http",
        name="HTTP Webhook",
        version="1.0.0",
        connector_type="automation",
        description="Approved outbound HTTPS event delivery transport.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.http_webhook",
        ),
        capabilities=(
            CapabilityManifest(
                id="events.deliver.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_attempts": {"type": "integer"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="signing_secret", required=False),
            SecretBindingManifest(name="authorization", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("events.outbound_projection",),
            emits=("events.external_delivery_receipt",),
            classifications=("domain_event_projection",),
        ),
        egress=EgressManifest(allow_installation_hosts=True),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    _dotmac_crm_manifest(
        version="1.1.0",
        include_chat_session=True,
    ),
    ConnectorManifest(
        key="whatsapp",
        name="WhatsApp",
        version="1.0.0",
        connector_type="messaging",
        description="Template and notification messaging connector.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.whatsapp_runtime",
        ),
        capabilities=(
            CapabilityManifest(
                id="messaging.send.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="messaging.receive.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="messaging.templates.read.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["meta_cloud_api"]},
                "phone_number": {"type": "string"},
                "waba_id": {"type": "string"},
                "webhook_url": {"type": "string"},
                "graph_version": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "templates": {"type": "array"},
            },
            "required": ["provider"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="service_credentials"),
            SecretBindingManifest(name="webhook_signing_secret", required=False),
            SecretBindingManifest(name="webhook_verify_token", required=False),
        ),
        data_access=DataAccessManifest(
            reads=("communications.outbound_message",),
            emits=("communications.inbound_message_observation",),
            classifications=("customer_contact", "message_content"),
        ),
        egress=EgressManifest(hosts=("graph.facebook.com",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="nextcloud.talk",
        name="Nextcloud Talk",
        version="1.0.0",
        connector_type="messaging",
        description="Staff collaboration notifications through Nextcloud Talk.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.nextcloud_talk",
        ),
        capabilities=(
            CapabilityManifest(
                id="collaboration.message.send.v1",
                modes=(CapabilityMode.event, CapabilityMode.interactive),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "notifier_username": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 30},
            },
            "required": ["url", "notifier_username"],
            "additionalProperties": False,
        },
        secrets=(SecretBindingManifest(name="app_password"),),
        data_access=DataAccessManifest(
            reads=("communications.staff_notification",),
            emits=("communications.external_delivery_receipt",),
            classifications=("staff_identity", "support_content", "message_content"),
        ),
        egress=EgressManifest(allow_installation_hosts=True),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    _meta_social_manifest(
        version="1.1.0",
        include_shared_oauth=True,
        include_auth_mode=True,
    ),
    _dotmac_erp_manifest(
        version="1.2.0",
        include_workforce_attendance=True,
        include_material_webhook=True,
    ),
    _paystack_manifest(
        version="1.0.1",
        include_safe_defaults=True,
        require_public_key=True,
    ),
    ConnectorManifest(
        key="flutterwave",
        name="Flutterwave",
        version="1.0.0",
        connector_type="payment",
        description="Online payment gateway integration.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.builtin_worker,
            module="app.services.integrations.connectors.payment_gateway",
        ),
        capabilities=(
            CapabilityManifest(
                id="payments.intent.v1",
                modes=(CapabilityMode.interactive, CapabilityMode.event),
            ),
            CapabilityManifest(
                id="payments.webhook.v1",
                modes=(CapabilityMode.inbound,),
            ),
            CapabilityManifest(
                id="payments.reconcile.v1",
                modes=(CapabilityMode.scheduled, CapabilityMode.reconcile),
            ),
            CapabilityManifest(
                id="payments.refund.v1",
                modes=(CapabilityMode.event, CapabilityMode.manual),
            ),
        ),
        config_schema={
            "type": "object",
            "properties": {
                "base_url": {
                    "type": "string",
                    "default": "https://api.flutterwave.com/v3",
                },
                "timeout_seconds": {"type": "integer", "default": 30},
                "default_currency": {"type": "string", "default": "NGN"},
            },
            "required": ["base_url"],
            "additionalProperties": False,
        },
        secrets=(
            SecretBindingManifest(name="gateway_credentials"),
            SecretBindingManifest(name="public_key", required=False),
            SecretBindingManifest(name="webhook_signing_secret"),
        ),
        data_access=DataAccessManifest(
            reads=("financial.payment_intent",),
            emits=("financial.payment_provider_observation",),
            classifications=("financial", "customer_contact"),
        ),
        egress=EgressManifest(hosts=("api.flutterwave.com",)),
        health=HealthManifest(operation="connection.validate.v1"),
    ),
    ConnectorManifest(
        key="3cx",
        name="3CX",
        version="1.0.0",
        connector_type="voice",
        description="Embedded PBX integration frame.",
        runtime=RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
    ),
    ConnectorManifest(
        key="freepbx",
        name="FreePBX",
        version="1.0.0",
        connector_type="voice",
        description="Embedded PBX integration frame.",
        runtime=RuntimeManifest(type=ConnectorRuntimeType.catalogue_only),
    ),
)

_HISTORICAL_DEFINITIONS: tuple[ConnectorManifest, ...] = (
    _dotmac_erp_manifest(version="1.1.0", include_workforce_attendance=True),
    # ERP 1.0.0 remains executable while installations explicitly adopt the
    # workforce attendance capability introduced in 1.1.0.
    _dotmac_erp_manifest(version="1.0.0", include_workforce_attendance=False),
    # CRM 1.0.0 remains executable while production adopts the explicit
    # temporary chat-session capability in 1.1.0.
    _dotmac_crm_manifest(version="1.0.0", include_chat_session=False),
    # The original Meta Social 1.0.0 pin did not declare the aggregate
    # auth_mode field. Production installations may retain this exact pin
    # until explicit adoption, so later manifest changes cannot rewrite it.
    _meta_social_manifest(
        version="1.0.0",
        include_shared_oauth=False,
        include_auth_mode=False,
    ),
    # A later 1.0.0 definition added individual auth_mode in place. Retain its
    # exact digest too because deployed pins are immutable compatibility facts.
    _meta_social_manifest(
        version="1.0.0",
        include_shared_oauth=False,
        include_auth_mode=True,
    ),
    # Pre-#1567 Paystack 1.0.0. Production installations created before the
    # payment control-plane cutover pin this exact digest.
    _paystack_manifest(
        version="1.0.0",
        include_safe_defaults=False,
        require_public_key=False,
    ),
    # #1567 changed 1.0.0 in place. Retain the transitional digest too so a
    # reviewed emergency adoption made before 1.0.1 remains executable.
    _paystack_manifest(
        version="1.0.0",
        include_safe_defaults=True,
        require_public_key=True,
    ),
)

_DEFINITION_BY_KEY = {definition.key: definition for definition in _DEFINITIONS}
if len(_DEFINITION_BY_KEY) != len(_DEFINITIONS):  # pragma: no cover - import guard
    raise RuntimeError("connector definition keys must be unique")
_SUPPORTED_DEFINITIONS = _DEFINITIONS + _HISTORICAL_DEFINITIONS
_DEFINITION_BY_PIN = {
    (definition.key, definition.version, definition.digest): definition
    for definition in _SUPPORTED_DEFINITIONS
}
if len(_DEFINITION_BY_PIN) != len(
    _SUPPORTED_DEFINITIONS
):  # pragma: no cover - import guard
    raise RuntimeError("connector definition pins must be unique")


def connector_definitions() -> tuple[ConnectorManifest, ...]:
    """Return the deterministic current connector catalogue."""

    return _DEFINITIONS


def supported_connector_definitions() -> tuple[ConnectorManifest, ...]:
    """Return current and bounded historical executable definitions."""

    return _SUPPORTED_DEFINITIONS


def connector_definition(key: str) -> ConnectorManifest | None:
    return _DEFINITION_BY_KEY.get(key.strip().lower())


def require_connector_definition(key: str) -> ConnectorManifest:
    definition = connector_definition(key)
    if definition is None:
        raise KeyError(f"unknown connector definition: {key}")
    return definition


def pinned_connector_definition(
    key: str,
    *,
    version: str,
    manifest_digest: str,
) -> ConnectorManifest | None:
    """Resolve only an exact deployed version/digest installation pin."""

    return _DEFINITION_BY_PIN.get(
        (
            key.strip().lower(),
            version.strip(),
            manifest_digest.strip().lower(),
        )
    )


def require_pinned_connector_definition(
    key: str,
    *,
    version: str,
    manifest_digest: str,
) -> ConnectorManifest:
    definition = pinned_connector_definition(
        key,
        version=version,
        manifest_digest=manifest_digest,
    )
    if definition is None:
        raise KeyError(
            "unknown connector definition pin: "
            f"{key.strip().lower()}@{version.strip()}#{manifest_digest.strip().lower()}"
        )
    return definition


def definitions_for_capability(capability_id: str) -> tuple[ConnectorManifest, ...]:
    return tuple(
        definition
        for definition in _DEFINITIONS
        if definition.capability(capability_id) is not None
    )


def _module_file_size(module_name: str | None) -> int:
    if not module_name:
        return 0
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return 0
    origin = spec.origin if spec is not None else None
    if not origin:
        return 0
    try:
        from pathlib import Path

        return int(Path(origin).stat().st_size)
    except OSError:
        return 0


def discover_connectors() -> list[ConnectorRegistryEntry]:
    """Project validated definitions into the legacy marketplace card shape.

    The function name remains for compatibility, but discovery is explicit and
    deterministic. Adding a file to the connectors directory no longer grants
    it catalogue presence or executable authority.
    """

    entries = [
        ConnectorRegistryEntry(
            key=definition.key,
            name=definition.name,
            version=definition.version,
            connector_type=definition.connector_type,
            description=definition.description,
            module_name=definition.runtime.module or f"catalog:{definition.key}",
            file_size_bytes=_module_file_size(definition.runtime.module),
        )
        for definition in _DEFINITIONS
        if definition.catalogue_visible
    ]
    return sorted(entries, key=lambda item: item.name.lower())
