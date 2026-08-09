"""Explicit, ordered assembly of every canonical SOT domain."""

from app.services.sot_registry.domains.ai_advisory import DOMAIN as AI_ADVISORY
from app.services.sot_registry.domains.application_sessions import (
    DOMAIN as APPLICATION_SESSIONS,
)
from app.services.sot_registry.domains.authorization_control_plane import (
    DOMAIN as AUTHORIZATION_CONTROL_PLANE,
)
from app.services.sot_registry.domains.customer_context import (
    DOMAIN as CUSTOMER_CONTEXT,
)
from app.services.sot_registry.domains.events_webhooks import DOMAIN as EVENTS_WEBHOOKS
from app.services.sot_registry.domains.feature_control_plane import (
    DOMAIN as FEATURE_CONTROL_PLANE,
)
from app.services.sot_registry.domains.financial_access import (
    DOMAIN as FINANCIAL_ACCESS,
)
from app.services.sot_registry.domains.geospatial import DOMAIN as GEOSPATIAL
from app.services.sot_registry.domains.integration_control_plane import (
    DOMAIN as INTEGRATION_CONTROL_PLANE,
)
from app.services.sot_registry.domains.network import DOMAIN as NETWORK
from app.services.sot_registry.domains.network_access_control_plane import (
    DOMAIN as NETWORK_ACCESS_CONTROL_PLANE,
)
from app.services.sot_registry.domains.notifications_communications import (
    DOMAIN as NOTIFICATIONS_COMMUNICATIONS,
)
from app.services.sot_registry.domains.observability import DOMAIN as OBSERVABILITY
from app.services.sot_registry.domains.party_identity import DOMAIN as PARTY_IDENTITY
from app.services.sot_registry.domains.provisioning_operations import (
    DOMAIN as PROVISIONING_OPERATIONS,
)
from app.services.sot_registry.domains.runtime_infrastructure import (
    DOMAIN as RUNTIME_INFRASTRUCTURE,
)
from app.services.sot_registry.domains.sales_referrals import DOMAIN as SALES_REFERRALS
from app.services.sot_registry.domains.scheduler_control_plane import (
    DOMAIN as SCHEDULER_CONTROL_PLANE,
)
from app.services.sot_registry.domains.secrets_credentials import (
    DOMAIN as SECRETS_CREDENTIALS,
)
from app.services.sot_registry.domains.service_intent_control_plane import (
    DOMAIN as SERVICE_INTENT_CONTROL_PLANE,
)
from app.services.sot_registry.domains.subscriber_sessions import (
    DOMAIN as SUBSCRIBER_SESSIONS,
)
from app.services.sot_registry.domains.support_operations import (
    DOMAIN as SUPPORT_OPERATIONS,
)
from app.services.sot_registry.domains.tenancy import DOMAIN as TENANCY
from app.services.sot_registry.domains.ui_action_forms import DOMAIN as UI_ACTION_FORMS
from app.services.sot_registry.domains.ui_bulk_actions import DOMAIN as UI_BULK_ACTIONS
from app.services.sot_registry.domains.ui_display_formatting import (
    DOMAIN as UI_DISPLAY_FORMATTING,
)
from app.services.sot_registry.domains.ui_list_projection import (
    DOMAIN as UI_LIST_PROJECTION,
)
from app.services.sot_registry.domains.ui_semantic_presentation import (
    DOMAIN as UI_SEMANTIC_PRESENTATION,
)
from app.services.sot_registry.domains.vpn_remote_access import (
    DOMAIN as VPN_REMOTE_ACCESS,
)
from app.services.sot_registry.domains.workforce_operations import (
    DOMAIN as WORKFORCE_OPERATIONS,
)

DOMAIN_DECLARATIONS = (
    PARTY_IDENTITY,
    CUSTOMER_CONTEXT,
    FINANCIAL_ACCESS,
    NETWORK,
    SUBSCRIBER_SESSIONS,
    APPLICATION_SESSIONS,
    SECRETS_CREDENTIALS,
    NOTIFICATIONS_COMMUNICATIONS,
    EVENTS_WEBHOOKS,
    RUNTIME_INFRASTRUCTURE,
    OBSERVABILITY,
    WORKFORCE_OPERATIONS,
    SUPPORT_OPERATIONS,
    TENANCY,
    AI_ADVISORY,
    PROVISIONING_OPERATIONS,
    FEATURE_CONTROL_PLANE,
    AUTHORIZATION_CONTROL_PLANE,
    SCHEDULER_CONTROL_PLANE,
    NETWORK_ACCESS_CONTROL_PLANE,
    SERVICE_INTENT_CONTROL_PLANE,
    INTEGRATION_CONTROL_PLANE,
    UI_LIST_PROJECTION,
    UI_BULK_ACTIONS,
    UI_DISPLAY_FORMATTING,
    UI_ACTION_FORMS,
    UI_SEMANTIC_PRESENTATION,
    VPN_REMOTE_ACCESS,
    GEOSPATIAL,
    SALES_REFERRALS,
)
