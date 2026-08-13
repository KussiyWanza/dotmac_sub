"""Web service helpers for remote ONT action routes.

This package provides services for ONT configuration, diagnostics,
and operational actions exposed through the admin web UI.

All existing import patterns are preserved for backward compatibility:
    from app.services import web_network_ont_actions as web_network_ont_actions_service
    from app.services.web_network_ont_actions import return_to_inventory
    from app.services.web_network_ont_actions import _cleanup_olt_state_for_return
"""

# Re-export private helpers that are imported externally
from app.services.web_network_ont_actions._common import (
    _actor_id_from_request,
    _config_snapshot_service,
    _current_user,
    _display_olt_value,
    _intent_saved_result,
    _is_input_error,
    _log_action_audit,
    _normalize_fsp,
    _parse_ont_id_on_olt,
    _persist_ont_plan_step,
    _resolve_return_olt_context,
    actor_name_from_request,
    cache_current_user_context,
)

# Configuration setters
from app.services.web_network_ont_actions.config_setters import (
    bind_internet_wan,
    bind_tr069_profile,
    configure_management_ip,
    force_push_wifi_password,
    force_resync_ont,
    set_connection_request_credentials,
    set_http_management,
    set_lan_config,
    set_mgmt_remote_access,
    set_pppoe_credentials,
    set_wan_config,
    set_wan_dhcp,
    set_wan_remote_access,
    set_wan_static,
    set_web_credentials,
    set_wifi_config,
    set_wifi_password,
    set_wifi_ssid,
    toggle_lan_port,
)

# Context builders
from app.services.web_network_ont_actions.context_builders import (
    configure_form_context,
    olt_side_config_context,
    olt_status_context,
    unified_config_context,
)

# Credentials
from app.services.web_network_ont_actions.credentials import (
    resolve_stored_pppoe_password,
    reveal_stored_pppoe_password,
)

# Database configuration
from app.services.web_network_ont_actions.db_config import (
    set_voip_enabled,
)

# Device actions
from app.services.web_network_ont_actions.device_actions import (
    QueuedRefreshResult,
    RunningConfigResult,
    execute_config_snapshot_refresh,
    execute_connection_request,
    execute_factory_reset,
    execute_omci_reboot,
    execute_reauthorize,
    execute_reboot,
    execute_refresh,
    fetch_olt_running_config,
    queue_refresh,
    refresh_operation_status,
)

# Diagnostics
from app.services.web_network_ont_actions.diagnostics import (
    fetch_iphost_config,
    fetch_running_config,
    run_ping_diagnostic,
    run_traceroute_diagnostic,
    running_config_context,
)

# Inventory management
from app.services.web_network_ont_actions.inventory import (
    _cleanup_olt_state_for_return,
    firmware_upgrade,
    return_to_inventory,
    return_to_inventory_for_web,
)

# Operational health and runbook
from app.services.web_network_ont_actions.operational import (
    fetch_olt_side_config,
    fetch_olt_status,
    operational_health_context,
    reconcile_operational_state,
)

# Config snapshots
from app.services.web_network_ont_actions.snapshots import (
    capture_config_snapshot_list_context,
    config_snapshot_detail_context,
    delete_config_snapshot_list_context,
    list_config_snapshots,
)

__all__ = [
    # Private helpers (needed by external modules)
    "_actor_id_from_request",
    "_cleanup_olt_state_for_return",
    "_config_snapshot_service",
    "_current_user",
    "_display_olt_value",
    "_intent_saved_result",
    "_is_input_error",
    "_log_action_audit",
    "_normalize_fsp",
    "_parse_ont_id_on_olt",
    "_persist_ont_plan_step",
    "_resolve_return_olt_context",
    # Public helpers
    "actor_name_from_request",
    "cache_current_user_context",
    # Credentials
    "resolve_stored_pppoe_password",
    "reveal_stored_pppoe_password",
    # Config snapshots
    "capture_config_snapshot_list_context",
    "config_snapshot_detail_context",
    "delete_config_snapshot_list_context",
    "list_config_snapshots",
    # Diagnostics
    "fetch_iphost_config",
    "fetch_running_config",
    "run_ping_diagnostic",
    "run_traceroute_diagnostic",
    "running_config_context",
    # Device actions
    "RunningConfigResult",
    "QueuedRefreshResult",
    "execute_config_snapshot_refresh",
    "execute_connection_request",
    "execute_factory_reset",
    "execute_omci_reboot",
    "execute_reauthorize",
    "execute_reboot",
    "execute_refresh",
    "fetch_olt_running_config",
    "queue_refresh",
    "refresh_operation_status",
    # Configuration setters
    "bind_tr069_profile",
    "bind_internet_wan",
    "configure_management_ip",
    "force_push_wifi_password",
    "force_resync_ont",
    "set_connection_request_credentials",
    "set_http_management",
    "set_lan_config",
    "set_mgmt_remote_access",
    "set_pppoe_credentials",
    "set_wan_config",
    "set_wan_dhcp",
    "set_wan_remote_access",
    "set_wan_static",
    "set_web_credentials",
    "set_wifi_config",
    "set_wifi_password",
    "set_wifi_ssid",
    "toggle_lan_port",
    # Context builders
    "configure_form_context",
    "olt_side_config_context",
    "olt_status_context",
    "unified_config_context",
    # Inventory
    "firmware_upgrade",
    "return_to_inventory",
    "return_to_inventory_for_web",
    # Operational
    "fetch_olt_side_config",
    "fetch_olt_status",
    "operational_health_context",
    "reconcile_operational_state",
    # Database config
    "set_voip_enabled",
]
