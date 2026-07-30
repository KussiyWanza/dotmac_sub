from app.tasks.admin_alerts import evaluate_infrastructure_alerts
from app.tasks.ai_operations import expire_stale_insights
from app.tasks.alert_evaluation import evaluate_alert_rules
from app.tasks.arrangements import check_overdue_arrangements
from app.tasks.autopay import charge_due_invoices
from app.tasks.bandwidth import (
    aggregate_to_metrics as aggregate_bandwidth_to_metrics,
)
from app.tasks.bandwidth import (
    cleanup_hot_data as cleanup_bandwidth_hot_data,
)
from app.tasks.bandwidth import (
    process_bandwidth_stream,
)
from app.tasks.bandwidth import (
    trim_redis_stream as trim_bandwidth_stream,
)
from app.tasks.billing import (
    audit_cutover_balance_invariant_task,
    audit_funded_inactive_exposure_task,
    check_billing_switch_task,
    run_invoice_cycle,
)
from app.tasks.campaigns import (
    process_due_campaign_steps,
    process_due_campaigns,
    send_campaign_batch,
)
from app.tasks.catalog import (
    apply_due_subscription_changes,
    apply_due_subscription_status_commands,
    expire_subscriptions,
)
from app.tasks.channel_health import observe_channel_health
from app.tasks.collections import prepaid_balance_sweep
from app.tasks.crm_ticket_pull import (
    pull_crm_tickets,
    sync_crm_ticket,
)
from app.tasks.cross_app_drift import run_cross_app_drift_detection
from app.tasks.customer_impact_metrics import export_customer_impact_metrics
from app.tasks.device_projection import reconcile_device_projections
from app.tasks.dotmac_erp_outbox import (
    deliver_erp_sync_events,
    refresh_expense_claim_statuses,
    refresh_material_request_statuses,
    refresh_purchase_invoice_statuses,
    repair_purchase_invoice_sync,
    sync_erp_operational_domains,
)
from app.tasks.durable_timers import fire_due_durable_timers
from app.tasks.enforcement import cleanup_subscription_block_sessions
from app.tasks.events import (
    cleanup_old_events,
    dispatch_pending_events,
    mark_stale_processing_events,
    retry_failed_events,
)
from app.tasks.exports import run_export_job, run_scheduled_export
from app.tasks.forwarding_control_observations import (
    run_forwarding_control_observation_poll,
)
from app.tasks.gis import run_batch_geocode_job, sync_gis_sources
from app.tasks.imports import run_import_job
from app.tasks.infrastructure_availability import (
    prune_infrastructure_availability,
    snapshot_infrastructure_availability,
)
from app.tasks.infrastructure_polling import run_infrastructure_poll
from app.tasks.integration_delivery import deliver_integration_event
from app.tasks.integrations import run_integration_job
from app.tasks.invoice_pdf import generate_invoice_pdf_export
from app.tasks.ip_utilization import (
    prune_ip_pool_utilization_snapshots,
    snapshot_ip_pool_utilization,
)
from app.tasks.monitoring_cleanup import (
    cleanup_old_device_metrics as cleanup_device_metrics,
)
from app.tasks.monitoring_cleanup import (
    sync_inventory_to_monitoring as sync_inventory_devices_to_monitoring,
)
from app.tasks.monitoring_cleanup import (
    sync_nas_to_monitoring as sync_nas_devices_to_monitoring,
)
from app.tasks.monitoring_coverage import refresh_monitoring_coverage
from app.tasks.mrr import snapshot_mrr
from app.tasks.nas import (
    check_nas_health,
    cleanup_nas_backups,
    run_scheduled_backups,
    update_subscriber_counts,
)
from app.tasks.network_operation_dispatch import (
    publish_network_operation_dispatches,
)
from app.tasks.network_operations import (
    cleanup_old_operations,
    publish_operation_metrics,
)
from app.tasks.notifications import deliver_notification_queue
from app.tasks.oauth import check_token_health, refresh_expiring_tokens
from app.tasks.olt_config_backup import backup_all_olts
from app.tasks.olt_firmware import rollback_firmware_task, upgrade_firmware_task
from app.tasks.olt_health_retry import (
    retry_failed_olt_connections,
    retry_single_olt,
    trigger_immediate_retry,
)
from app.tasks.olt_mac_harvest import (
    run_olt_mac_harvest,
    run_single_olt_mac_harvest,
)
from app.tasks.ont_bulk import execute_bulk_action as execute_ont_bulk_action
from app.tasks.ont_commissioning import (
    cleanup_commissioned_ont,
    commission_ont,
    verify_commissioned_ont,
)
from app.tasks.ont_commissioning import (
    reconcile_intents as reconcile_ont_commissioning_intents,
)
from app.tasks.ont_firmware import (
    apply_huawei_ont_firmware,
    verify_huawei_ont_firmware,
)
from app.tasks.ont_provisioning import (
    authorize_ont as authorize_ont_task,
)
from app.tasks.ont_provisioning import (
    provision_ont,
    queue_bulk_provisioning,
)
from app.tasks.ont_reconcile import run_ont_reconcile_sweep
from app.tasks.ont_runtime_status import (
    dispatch_huawei_ont_status,
    refresh_huawei_olt_status,
)
from app.tasks.ont_signal_observations import record_ont_observations
from app.tasks.operational_escalations import dispatch_operational_escalation_deliveries
from app.tasks.outage_auto_notify import auto_dispatch_outage_notifications
from app.tasks.payment_reconciliation import reconcile_topups
from app.tasks.profile_sync import (
    execute_due_profile_sync_tasks,
)
from app.tasks.provisioning import (
    reap_stale_provisioning_runs,
    retry_pending_compensation_failures,
    run_bulk_activation_job,
    run_service_migration_job,
)
from app.tasks.quotes import reconcile_quote_mirror
from app.tasks.radius import run_radius_sync_job
from app.tasks.radius_health import run_radius_health_check
from app.tasks.radius_population import refresh_radius_from_subs, sync_device_login
from app.tasks.referrals import reconcile_referral_mirror
from app.tasks.reports import send_scheduled_ncc_report
from app.tasks.router_sync import (
    audit_sot_drift,
    capture_scheduled_snapshots,
    cleanup_idle_tunnels,
    execute_config_push,
    reconcile_config_push_readback,
    reconcile_nas_vlan_readback,
    sync_all_interfaces,
    sync_all_system_info,
)
from app.tasks.security import run_scheduled_credential_rotation
from app.tasks.support_tickets import auto_confirm_resolved_tickets
from app.tasks.team_inbox import (
    auto_resolve_stale_conversations as auto_resolve_stale_inbox_conversations,
)
from app.tasks.team_inbox import (
    promote_message_media_assets as promote_inbox_message_media_assets,
)
from app.tasks.team_inbox import (
    retry_failed_outbound_messages as retry_failed_inbox_outbound_messages,
)
from app.tasks.topology_lldp import run_lldp_topology_poll
from app.tasks.topology_metrics import export_topology_metrics
from app.tasks.topology_outage import reconcile_detected_outages
from app.tasks.topology_sync import warm_topology_status
from app.tasks.topology_ufiber_link import run_ufiber_onu_link
from app.tasks.topology_uisp import run_uisp_topology_sync
from app.tasks.tr069 import (
    apply_acs_config as tr069_apply_acs_config,
)
from app.tasks.tr069 import (
    check_device_health as tr069_check_device_health,
)
from app.tasks.tr069 import (
    cleanup_tr069_records,
)
from app.tasks.tr069 import (
    execute_network_operation_job as tr069_execute_network_operation_job,
)
from app.tasks.tr069 import (
    reconcile_command_outcomes as tr069_reconcile_command_outcomes,
)
from app.tasks.tr069 import (
    refresh_ont_runtime_data as tr069_refresh_ont_runtime,
)
from app.tasks.tr069 import (
    refresh_single_ont_runtime as tr069_refresh_single_ont,
)
from app.tasks.tr069 import (
    sync_all_acs_devices as tr069_sync_all_acs_devices,
)
from app.tasks.uisp_control import apply_uisp_intent, reconcile_uisp_config_readback
from app.tasks.uisp_ip_backfill import run_uisp_mgmt_ip_backfill
from app.tasks.unmatched_radio import run_unmatched_radio_review
from app.tasks.usage import (
    import_radius_accounting,
    notify_expiring_data_bundles,
    reap_stale_radius_sessions,
    run_usage_rating,
)
from app.tasks.vacation_holds import resume_expired_holds
from app.tasks.vpn import run_vpn_control_job, run_vpn_health_scan
from app.tasks.wireguard import (
    cleanup_connection_logs as cleanup_wireguard_logs,
)
from app.tasks.wireguard import (
    cleanup_expired_tokens as cleanup_wireguard_tokens,
)
from app.tasks.wireguard import (
    generate_connection_log_report as wireguard_connection_report,
)
from app.tasks.workflow import detect_sla_breaches as retired_detect_sla_breaches

__all__ = [
    "cleanup_old_operations",
    "publish_network_operation_dispatches",
    "publish_operation_metrics",
    "sync_gis_sources",
    "run_batch_geocode_job",
    "run_import_job",
    "run_integration_job",
    "deliver_integration_event",
    "process_due_campaigns",
    "process_due_campaign_steps",
    "send_campaign_batch",
    "expire_stale_insights",
    "generate_invoice_pdf_export",
    "run_radius_sync_job",
    "provision_ont",
    "queue_bulk_provisioning",
    "commission_ont",
    "verify_commissioned_ont",
    "cleanup_commissioned_ont",
    "reconcile_ont_commissioning_intents",
    "record_ont_observations",
    "run_invoice_cycle",
    "charge_due_invoices",
    "check_overdue_arrangements",
    "reconcile_topups",
    "expire_subscriptions",
    "apply_due_subscription_changes",
    "apply_due_subscription_status_commands",
    "prepaid_balance_sweep",
    "audit_cutover_balance_invariant_task",
    "audit_funded_inactive_exposure_task",
    "check_billing_switch_task",
    "pull_crm_tickets",
    "sync_crm_ticket",
    "auto_confirm_resolved_tickets",
    "retry_failed_inbox_outbound_messages",
    "promote_inbox_message_media_assets",
    "auto_resolve_stale_inbox_conversations",
    "run_scheduled_export",
    "run_export_job",
    "retry_failed_events",
    "dispatch_pending_events",
    "mark_stale_processing_events",
    "cleanup_old_events",
    "cleanup_subscription_block_sessions",
    "run_cross_app_drift_detection",
    "deliver_erp_sync_events",
    "refresh_expense_claim_statuses",
    "refresh_material_request_statuses",
    "refresh_purchase_invoice_statuses",
    "repair_purchase_invoice_sync",
    "sync_erp_operational_domains",
    "run_usage_rating",
    "import_radius_accounting",
    "reap_stale_radius_sessions",
    "reap_stale_provisioning_runs",
    "notify_expiring_data_bundles",
    "cleanup_nas_backups",
    "refresh_expiring_tokens",
    "check_token_health",
    "cleanup_wireguard_logs",
    "cleanup_wireguard_tokens",
    "wireguard_connection_report",
    "process_bandwidth_stream",
    "cleanup_bandwidth_hot_data",
    "aggregate_bandwidth_to_metrics",
    "trim_bandwidth_stream",
    "backup_all_olts",
    "rollback_firmware_task",
    "upgrade_firmware_task",
    "run_olt_mac_harvest",
    "run_single_olt_mac_harvest",
    "dispatch_operational_escalation_deliveries",
    "fire_due_durable_timers",
    "retry_failed_olt_connections",
    "retry_single_olt",
    "trigger_immediate_retry",
    "run_bulk_activation_job",
    "run_service_migration_job",
    "retry_pending_compensation_failures",
    "refresh_radius_from_subs",
    "sync_device_login",
    "run_vpn_control_job",
    "run_vpn_health_scan",
    "deliver_notification_queue",
    "observe_channel_health",
    "snapshot_mrr",
    "snapshot_ip_pool_utilization",
    "snapshot_infrastructure_availability",
    "prune_infrastructure_availability",
    "prune_ip_pool_utilization_snapshots",
    "warm_topology_status",
    "run_infrastructure_poll",
    "export_customer_impact_metrics",
    "reconcile_device_projections",
    "run_radius_health_check",
    "run_lldp_topology_poll",
    "run_forwarding_control_observation_poll",
    "reconcile_detected_outages",
    "auto_dispatch_outage_notifications",
    "run_uisp_topology_sync",
    "run_uisp_mgmt_ip_backfill",
    "apply_uisp_intent",
    "reconcile_uisp_config_readback",
    "run_ufiber_onu_link",
    "run_unmatched_radio_review",
    "export_topology_metrics",
    "tr069_sync_all_acs_devices",
    "tr069_reconcile_command_outcomes",
    "tr069_execute_network_operation_job",
    "tr069_apply_acs_config",
    "tr069_check_device_health",
    "tr069_refresh_ont_runtime",
    "tr069_refresh_single_ont",
    "cleanup_tr069_records",
    "run_scheduled_backups",
    "update_subscriber_counts",
    "check_nas_health",
    "execute_ont_bulk_action",
    "run_ont_reconcile_sweep",
    "apply_huawei_ont_firmware",
    "verify_huawei_ont_firmware",
    "dispatch_huawei_ont_status",
    "refresh_huawei_olt_status",
    "authorize_ont_task",
    "evaluate_alert_rules",
    "evaluate_infrastructure_alerts",
    "cleanup_device_metrics",
    "sync_nas_devices_to_monitoring",
    "sync_inventory_devices_to_monitoring",
    "retired_detect_sla_breaches",
    "resume_expired_holds",
    # OLT queue processing
    "execute_due_profile_sync_tasks",
    "refresh_monitoring_coverage",
    # Router config sync/snapshot (keystone) — previously unregistered, so the
    # scheduled capture never ran. Importing here registers them with the worker.
    "capture_scheduled_snapshots",
    "audit_sot_drift",
    "cleanup_idle_tunnels",
    "execute_config_push",
    "reconcile_config_push_readback",
    "reconcile_nas_vlan_readback",
    "sync_all_interfaces",
    "sync_all_system_info",
    "run_scheduled_credential_rotation",
    "reconcile_quote_mirror",
    "reconcile_referral_mirror",
    "send_scheduled_ncc_report",
]
