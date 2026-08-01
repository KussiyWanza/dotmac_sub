"""NAS device management service package.

Provides CRUD operations and business logic for:
- NAS Device inventory management
- Configuration backup and restore
- Provisioning templates and execution
- Connection rules
- RADIUS profiles
- MikroTik vendor integration

All public symbols are re-exported here for backward compatibility.
Import via ``from app.services.nas import X`` or
``from app.services import nas as nas_service``.
"""

# Manager classes
# Helpers and utilities
# Re-export schemas that were available on the old monolithic module
from app.schemas.catalog import (  # noqa: F401
    NasConfigBackupCreate,
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningLogCreate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services import backup_alerts as backup_alerts_service  # noqa: F401

# Re-export commonly monkeypatched dependencies for test compatibility
from app.services import ping as ping_service  # noqa: F401
from app.services.nas import local_secret_policy
from app.services.nas._helpers import (
    RADIUS_REQUIRED_CONNECTION_TYPES,
    TEMPLATE_AUDIT_EXCLUDE_FIELDS,
    _emit_nas_event,
    _redact_sensitive,
    extract_enhanced_fields,
    extract_mikrotik_status,
    get_nas_form_options,
    get_pop_site,
    list_business_accounts,
    list_pop_sites,
    merge_partner_org_tags,
    merge_radius_pool_tags,
    merge_single_tag,
    pop_site_label,
    pop_site_label_by_id,
    prefixed_value_from_tags,
    prefixed_values_from_tags,
    radius_pool_ids_from_tags,
    resolve_partner_org_names,
    resolve_radius_pool_names,
    upsert_prefixed_tags,
    validate_ipv4_address,
)

# MikroTik vendor helpers
from app.services.nas._mikrotik import (
    generate_mikrotik_bootstrap_script_for_device,
    get_mikrotik_api_status,
    get_mikrotik_api_telemetry,
    get_mikrotik_pppoe_live_bandwidth,
    refresh_mikrotik_status_for_device,
)
from app.services.nas.backups import NasConfigBackups
from app.services.nas.connection_rules import (
    NasConnectionRules,
    create_connection_rule_for_device,
    delete_connection_rule_for_device,
    toggle_connection_rule_for_device,
)
from app.services.nas.devices import NasDevices
from app.services.nas.logs import ProvisioningLogs
from app.services.nas.profiles import RadiusProfiles
from app.services.nas.provisioner import DeviceProvisioner
from app.services.nas.templates import ProvisioningTemplates

# Web context builders
from app.services.nas.web_builders import (
    NAS_LIST_DEFINITION,
    build_nas_backup_compare_data,
    build_nas_backup_detail_data,
    build_nas_dashboard_data,
    build_nas_device_backups_page_data,
    build_nas_device_detail_data,
    build_nas_device_payload,
    build_nas_list_query,
    build_nas_log_detail_data,
    build_nas_logs_list_data,
    build_nas_template_form_data,
    build_nas_templates_list_data,
    build_provisioning_template_payload,
    create_connection_rule_redirect_url,
    create_nas_device_with_audit,
    create_provisioning_template_with_audit,
    create_provisioning_template_with_metadata,
    delete_connection_rule_redirect_url,
    delete_nas_device_with_audit,
    delete_provisioning_template_with_audit,
    enable_monitoring_redirect_url,
    get_cached_ping_status,
    get_ping_status,
    live_bandwidth_redirect_url,
    mikrotik_api_test_redirect_url,
    ping_nas_device_and_touch_last_seen,
    sync_all_monitoring_redirect_url,
    toggle_connection_rule_redirect_url,
    trigger_backup_for_device,
    trigger_backup_for_device_with_audit,
    update_nas_device_with_audit,
    update_provisioning_template_with_audit,
    update_provisioning_template_with_metadata,
)

# Singleton instances
nas_devices = NasDevices()
nas_config_backups = NasConfigBackups()
provisioning_templates = ProvisioningTemplates()
provisioning_logs = ProvisioningLogs()
radius_profiles = RadiusProfiles()
device_provisioner = DeviceProvisioner()

__all__ = [
    # Classes
    "NasDevices",
    "NasConfigBackups",
    "NasConnectionRules",
    "ProvisioningTemplates",
    "ProvisioningLogs",
    "RadiusProfiles",
    "DeviceProvisioner",
    "local_secret_policy",
    # Singletons
    "nas_devices",
    "nas_config_backups",
    "provisioning_templates",
    "provisioning_logs",
    "radius_profiles",
    "device_provisioner",
    # Connection rule helpers
    "create_connection_rule_for_device",
    "toggle_connection_rule_for_device",
    "delete_connection_rule_for_device",
    # Helper functions
    "_emit_nas_event",
    "_redact_sensitive",
    "RADIUS_REQUIRED_CONNECTION_TYPES",
    "TEMPLATE_AUDIT_EXCLUDE_FIELDS",
    "list_pop_sites",
    "get_pop_site",
    "list_business_accounts",
    "get_nas_form_options",
    "validate_ipv4_address",
    "prefixed_values_from_tags",
    "prefixed_value_from_tags",
    "radius_pool_ids_from_tags",
    "upsert_prefixed_tags",
    "merge_single_tag",
    "merge_radius_pool_tags",
    "merge_partner_org_tags",
    "extract_enhanced_fields",
    "extract_mikrotik_status",
    "resolve_radius_pool_names",
    "resolve_partner_org_names",
    "pop_site_label",
    "pop_site_label_by_id",
    # MikroTik
    "get_mikrotik_api_status",
    "get_mikrotik_api_telemetry",
    "get_mikrotik_pppoe_live_bandwidth",
    "refresh_mikrotik_status_for_device",
    "generate_mikrotik_bootstrap_script_for_device",
    # Web builders
    "build_nas_device_payload",
    "build_provisioning_template_payload",
    "create_provisioning_template_with_metadata",
    "update_provisioning_template_with_metadata",
    "build_nas_dashboard_data",
    "build_nas_list_query",
    "NAS_LIST_DEFINITION",
    "build_nas_device_detail_data",
    "build_nas_device_backups_page_data",
    "build_nas_backup_detail_data",
    "build_nas_backup_compare_data",
    "build_nas_templates_list_data",
    "build_nas_template_form_data",
    "build_nas_logs_list_data",
    "build_nas_log_detail_data",
    "create_nas_device_with_audit",
    "update_nas_device_with_audit",
    "delete_nas_device_with_audit",
    "ping_nas_device_and_touch_last_seen",
    "live_bandwidth_redirect_url",
    "create_connection_rule_redirect_url",
    "toggle_connection_rule_redirect_url",
    "delete_connection_rule_redirect_url",
    "mikrotik_api_test_redirect_url",
    "create_provisioning_template_with_audit",
    "update_provisioning_template_with_audit",
    "delete_provisioning_template_with_audit",
    "enable_monitoring_redirect_url",
    "sync_all_monitoring_redirect_url",
    "trigger_backup_for_device",
    "trigger_backup_for_device_with_audit",
    "get_ping_status",
    "get_cached_ping_status",
]
