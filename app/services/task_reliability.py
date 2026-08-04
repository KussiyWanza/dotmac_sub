"""Reliability contracts for Celery tasks.

Celery is the transport layer. This registry documents the retry and failure
handling contract for every first-party task so new tasks cannot be added
without an explicit reliability decision.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class RetryPolicy(StrEnum):
    """Primary retry strategy for a task."""

    BEAT_RERUN = "beat_rerun"
    CELERY_AUTORETRY = "celery_autoretry"
    DB_STATE_MACHINE = "db_state_machine"
    DEAD_LETTER_REDRIVE = "dead_letter_redrive"
    ITEM_LEVEL = "item_level"
    MANUAL_REDRIVE = "manual_redrive"
    NO_RETRY = "no_retry"


class Idempotency(StrEnum):
    """How safe a repeat execution is expected to be."""

    IDEMPOTENT = "idempotent"
    GUARDED = "guarded"
    STATE_MACHINE = "state_machine"
    PER_ITEM_GUARDED = "per_item_guarded"
    NON_IDEMPOTENT = "non_idempotent"


class FailureVisibility(StrEnum):
    """Where operators can see or recover failures."""

    LOG_ONLY = "log_only"
    HEALTH_HEARTBEAT = "health_heartbeat"
    DOMAIN_STATUS = "domain_status"
    DEAD_LETTER = "dead_letter"
    ADMIN_REDRIVE = "admin_redrive"


@dataclass(frozen=True)
class TaskReliabilityContract:
    domain: str
    retry_policy: RetryPolicy
    idempotency: Idempotency
    failure_visibility: FailureVisibility
    notes: str = ""


def _c(
    domain: str,
    retry_policy: RetryPolicy,
    idempotency: Idempotency,
    failure_visibility: FailureVisibility,
    notes: str = "",
) -> TaskReliabilityContract:
    return TaskReliabilityContract(
        domain=domain,
        retry_policy=retry_policy,
        idempotency=idempotency,
        failure_visibility=failure_visibility,
        notes=notes,
    )


SWEEP = RetryPolicy.BEAT_RERUN
AUTORETRY = RetryPolicy.CELERY_AUTORETRY
STATE = RetryPolicy.DB_STATE_MACHINE
DLQ = RetryPolicy.DEAD_LETTER_REDRIVE
ITEMS = RetryPolicy.ITEM_LEVEL
MANUAL = RetryPolicy.MANUAL_REDRIVE
NONE = RetryPolicy.NO_RETRY

IDEMP = Idempotency.IDEMPOTENT
GUARDED = Idempotency.GUARDED
STATEFUL = Idempotency.STATE_MACHINE
PER_ITEM = Idempotency.PER_ITEM_GUARDED
NON_IDEMP = Idempotency.NON_IDEMPOTENT

LOG = FailureVisibility.LOG_ONLY
HEALTH = FailureVisibility.HEALTH_HEARTBEAT
STATUS = FailureVisibility.DOMAIN_STATUS
DEAD = FailureVisibility.DEAD_LETTER
REDRIVE = FailureVisibility.ADMIN_REDRIVE


TASK_RELIABILITY_CONTRACTS: dict[str, TaskReliabilityContract] = {
    "app.tasks.admin_alerts.evaluate_infrastructure_alerts": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.ai_operations.expire_stale_insights": _c(
        "ai",
        SWEEP,
        IDEMP,
        STATUS,
        "Expires stale native AI insights; expired rows are skipped on re-run.",
    ),
    "app.tasks.alert_evaluation.evaluate_alert_rules": _c(
        "monitoring", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.arrangements.check_overdue_arrangements": _c(
        "billing", SWEEP, GUARDED, HEALTH
    ),
    "app.tasks.autopay.charge_due_invoices": _c(
        "billing", STATE, GUARDED, STATUS, "Money-moving; retry through payment state."
    ),
    "app.tasks.bandwidth.aggregate_to_metrics": _c("bandwidth", SWEEP, IDEMP, HEALTH),
    "app.tasks.bandwidth.cleanup_hot_data": _c("bandwidth", SWEEP, IDEMP, LOG),
    "app.tasks.bandwidth.process_bandwidth_stream": _c(
        "bandwidth", SWEEP, PER_ITEM, HEALTH
    ),
    "app.tasks.bandwidth.trim_redis_stream": _c("bandwidth", SWEEP, IDEMP, LOG),
    "app.tasks.billing.audit_cutover_balance_invariant": _c(
        "billing", SWEEP, IDEMP, HEALTH, "Read-only drift audit; safe to re-run."
    ),
    "app.tasks.billing.audit_funded_inactive_exposure": _c(
        "billing",
        SWEEP,
        IDEMP,
        HEALTH,
        "Read-only funded inactive liability report; safe to re-run.",
    ),
    "app.tasks.billing.check_billing_switch": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.billing.mark_invoices_overdue": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.billing.refresh_billing_health_snapshot": _c(
        "billing",
        SWEEP,
        IDEMP,
        HEALTH,
        "Read-only single-flight snapshot producer; the next beat run repairs "
        "a missing or stale snapshot.",
    ),
    "app.tasks.billing.run_billing_notifications": _c(
        "billing", STATE, GUARDED, STATUS
    ),
    "app.tasks.billing.run_invoice_cycle": _c(
        "billing", STATE, GUARDED, HEALTH, "Invoice creation must remain idempotent."
    ),
    "app.tasks.catalog.expire_subscriptions": _c("catalog", SWEEP, GUARDED, HEALTH),
    "app.tasks.catalog.send_expiry_reminders": _c("catalog", SWEEP, GUARDED, STATUS),
    "app.tasks.catalog.apply_due_subscription_changes": _c(
        "catalog",
        SWEEP,
        GUARDED,
        HEALTH,
        "Applying a scheduled plan change must be idempotent (apply() status guard).",
    ),
    "app.tasks.catalog.apply_due_subscription_status_commands": _c(
        "catalog",
        STATE,
        STATEFUL,
        STATUS,
        "Durable schedule rows own leases, bounded retry, reviewed-head drift "
        "detection, and deterministic executor idempotency keys.",
    ),
    "app.tasks.channel_health.observe_channel_health": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        HEALTH,
        "Permanent verification input: publishes per-channel silence gauges "
        "from team-inbox facts and recomputes the full snapshot on every run.",
    ),
    "app.tasks.campaigns.process_due_campaigns": _c(
        "campaigns",
        SWEEP,
        PER_ITEM,
        STATUS,
        "Builds recipients and sends due campaigns through native inbox channels; "
        "recipient rows gate repeat sends. The runner is permanent after scheduling "
        "admission so accepted campaigns cannot be stranded.",
    ),
    "app.tasks.campaigns.process_due_campaign_steps": _c(
        "campaigns",
        SWEEP,
        PER_ITEM,
        STATUS,
        "Materializes and sends the next due step of a nurture sequence; a step "
        "that already has recipient rows is never materialized twice. Accepted "
        "sequence intent drains independently of the scheduling admission gate.",
    ),
    "app.tasks.campaigns.send_campaign_batch": _c(
        "campaigns",
        ITEMS,
        PER_ITEM,
        STATUS,
        "Sends pending campaign recipients only; terminal recipients are skipped.",
    ),
    "app.tasks.collections.prepaid_balance_sweep": _c(
        "collections",
        SWEEP,
        GUARDED,
        HEALTH,
        "Daily balance sweep; per-account commit, idempotent arm/warn/suspend.",
    ),
    "app.tasks.collections.run_billing_enforcement": _c(
        "collections", STATE, GUARDED, HEALTH
    ),
    "app.tasks.collections.run_bundle_reconcile": _c(
        "collections", STATE, GUARDED, HEALTH
    ),
    "app.tasks.crm_ticket_pull.pull_crm_tickets": _c("crm", SWEEP, IDEMP, HEALTH),
    "app.tasks.crm_ticket_pull.sync_crm_ticket": _c("crm", SWEEP, IDEMP, STATUS),
    "app.tasks.cross_app_drift.run_cross_app_drift_detection": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        STATUS,
        "Read-only; findings deduped by fingerprint.",
    ),
    "app.tasks.device_projection.reconcile_device_projections": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        HEALTH,
        "Permanent idempotent projection rebuild; the next reconcile repairs a "
        "stale or partial device_projections table and prunes orphans.",
    ),
    "app.tasks.dotmac_erp_outbox.deliver_erp_sync_events": _c(
        "integration",
        STATE,
        GUARDED,
        DEAD,
        "ERP outbox delivery (money path). Per-row DB state machine; stored + "
        "sent idempotency key makes re-delivery safe; single-writer gated by "
        "sync_flow_ownership; transient rows stay pending, permanent/budget-"
        "exhausted rows dead-letter in field_erp_sync_events.",
    ),
    "app.tasks.dotmac_erp_outbox.refresh_expense_claim_statuses": _c(
        "integration",
        SWEEP,
        IDEMP,
        STATUS,
        "Read-only ERP status poll for in-flight expense claims; re-run safe, "
        "refreshes expense_claim_status on the source row.",
    ),
    "app.tasks.dotmac_erp_outbox.refresh_material_request_statuses": _c(
        "integration",
        SWEEP,
        IDEMP,
        STATUS,
        "Read-only ERP status poll for in-flight material requests; re-run safe, "
        "refreshes support_status on the source row.",
    ),
    "app.tasks.dotmac_erp_outbox.repair_purchase_invoice_sync": _c(
        "integration", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.dotmac_erp_outbox.refresh_purchase_invoice_statuses": _c(
        "integration",
        SWEEP,
        IDEMP,
        STATUS,
        "ERP-owned AP status poll; validates identity/currency/amounts, retains "
        "the last good observation, and records per-invoice refresh errors.",
    ),
    "app.tasks.dotmac_erp_outbox.sync_erp_operational_domains": _c(
        "integration",
        SWEEP,
        IDEMP,
        STATUS,
        "Watermarked Sub project/ticket/work-order context feed to ERP; ERP upserts by source UUID.",
    ),
    "app.tasks.enforcement.cleanup_subscription_block_sessions": _c(
        "enforcement", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.enforcement.reconcile_billing_approval_drift": _c(
        "enforcement",
        SWEEP,
        PER_ITEM,
        LOG,
        "Permanent drift sweep; each account is locked and recomputed by the "
        "billing-approval owner, failures are isolated, and the next beat pass "
        "reselects any remaining active/unapproved account.",
    ),
    "app.tasks.enforcement.detect_stale_overdue_locks": _c(
        "enforcement", SWEEP, IDEMP, STATUS, "Dry-run detector; writes nothing."
    ),
    "app.tasks.events.cleanup_old_events": _c("events", SWEEP, IDEMP, LOG),
    "app.tasks.events.dispatch_pending_events": _c(
        "events",
        SWEEP,
        PER_ITEM,
        STATUS,
        "Beat-rerun drains committed outbox rows; per-event row state gates "
        "delivery and records item-level failures.",
    ),
    "app.tasks.reports.send_scheduled_ncc_report": _c(
        "reporting",
        SWEEP,
        GUARDED,
        HEALTH,
        "Weekly single-flight digest; the persisted local send-date prevents "
        "duplicate delivery and the next beat run repairs a failed attempt.",
    ),
    "app.tasks.events.mark_stale_processing_events": _c("events", SWEEP, IDEMP, STATUS),
    "app.tasks.events.retry_failed_events": _c("events", STATE, STATEFUL, STATUS),
    "app.tasks.exports.run_export_job": _c("exports", MANUAL, GUARDED, STATUS),
    "app.tasks.exports.run_scheduled_export": _c("exports", SWEEP, GUARDED, STATUS),
    "app.tasks.gis.run_batch_geocode_job": _c("gis", ITEMS, PER_ITEM, STATUS),
    "app.tasks.gis.sync_gis_sources": _c("gis", SWEEP, IDEMP, HEALTH),
    "app.tasks.imports.process_import_run": _c("imports", ITEMS, PER_ITEM, STATUS),
    "app.tasks.imports.run_import_job": _c("imports", ITEMS, PER_ITEM, STATUS),
    "app.tasks.infrastructure_availability.prune_infrastructure_availability": _c(
        "monitoring", SWEEP, IDEMP, LOG
    ),
    "app.tasks.infrastructure_availability.snapshot_infrastructure_availability": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.integrations.run_integration_job": _c(
        "integrations", STATE, GUARDED, STATUS
    ),
    "app.tasks.integration_delivery.deliver_integration_event": _c(
        "integrations",
        STATE,
        STATEFUL,
        REDRIVE,
        "Persisted delivery identity and state gate retries; terminal failures "
        "are visible and replayable from integration administration.",
    ),
    "app.tasks.invoice_pdf.generate_invoice_pdf_export": _c(
        "billing", MANUAL, IDEMP, STATUS
    ),
    "app.tasks.ip_utilization.prune_ip_pool_utilization_snapshots": _c(
        "ipam", SWEEP, IDEMP, LOG
    ),
    "app.tasks.ip_utilization.snapshot_ip_pool_utilization": _c(
        "ipam", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.monitoring_cleanup.check_stale_infrastructure": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.monitoring_cleanup.cleanup_old_device_metrics": _c(
        "monitoring", SWEEP, IDEMP, LOG
    ),
    "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        STATUS,
        "Permanent inventory projection repair for device verification.",
    ),
    "app.tasks.monitoring_cleanup.sync_nas_to_monitoring": _c(
        "monitoring", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.monitoring_coverage.refresh_monitoring_coverage": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        HEALTH,
        "Permanent verification-path observation; recomputes reachable CIDRs.",
    ),
    "app.tasks.mrr.snapshot_mrr": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.nas.check_nas_health": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.nas.cleanup_nas_backups": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.nas.run_scheduled_backups": _c("network", STATE, GUARDED, STATUS),
    "app.tasks.nas.update_subscriber_counts": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.network_operations.cleanup_old_operations": _c(
        "network", SWEEP, IDEMP, LOG
    ),
    "app.tasks.network_operations.publish_operation_metrics": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.network_operation_dispatch.publish_network_operation_dispatches": _c(
        "network",
        SWEEP,
        STATEFUL,
        STATUS,
        "Transactional outbox publisher; row state owns retries and unknown delivery.",
    ),
    "app.tasks.nin_tasks.verify_nin_task": _c("identity", AUTORETRY, GUARDED, STATUS),
    "app.tasks.notifications.deliver_notification_queue": _c(
        "notifications", STATE, GUARDED, STATUS
    ),
    "app.tasks.oauth.check_token_health": _c("integrations", SWEEP, IDEMP, HEALTH),
    "app.tasks.oauth.refresh_expiring_tokens": _c(
        "integrations", STATE, GUARDED, STATUS
    ),
    "app.tasks.olt_config_backup.backup_all_olts": _c("network", SWEEP, IDEMP, STATUS),
    "app.tasks.olt_health_retry.retry_failed_olt_connections": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.olt_health_retry.retry_single_olt": _c(
        "network", AUTORETRY, IDEMP, STATUS
    ),
    "app.tasks.olt_mac_harvest.run_olt_mac_harvest": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.olt_mac_harvest.run_single_olt_mac_harvest": _c(
        "network",
        AUTORETRY,
        PER_ITEM,
        HEALTH,
        "Each OLT has an independent advisory lock, timeout, and retry budget.",
    ),
    "app.tasks.olt_firmware.rollback": _c(
        "network",
        NONE,
        NON_IDEMP,
        LOG,
        "Device rollback is a non-idempotent operator action and is never retried "
        "automatically.",
    ),
    "app.tasks.olt_firmware.upgrade_with_verification": _c(
        "network",
        STATE,
        STATEFUL,
        STATUS,
        "The dispatch outbox owns single execution admission; the operation ledger "
        "owns progress and terminal device outcome.",
    ),
    "app.tasks.ont_bulk.execute_bulk_action": _c("network", ITEMS, PER_ITEM, STATUS),
    "app.tasks.ont_firmware.apply_huawei_ont_firmware": _c(
        "network",
        STATE,
        GUARDED,
        STATUS,
        "The dispatch outbox admits one execution; the delivery-start marker also "
        "switches recovery to readback instead of reflashing.",
    ),
    "app.tasks.ont_firmware.verify_huawei_ont_firmware": _c(
        "network",
        STATE,
        STATEFUL,
        STATUS,
        "Bounded delayed readback retries are owned by the operation ledger.",
    ),
    "app.tasks.ont_provisioning.authorize_ont": _c(
        "provisioning", STATE, STATEFUL, STATUS
    ),
    "app.tasks.ont_provisioning.provision_ont": _c(
        "provisioning", STATE, STATEFUL, STATUS
    ),
    "app.tasks.ont_provisioning.queue_bulk_provisioning": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.ont_commissioning.commission_ont": _c(
        "provisioning",
        STATE,
        STATEFUL,
        STATUS,
        "Exact live autofind admission and landed-device evidence prevent blind writes.",
    ),
    "app.tasks.ont_commissioning.verify_commissioned_ont": _c(
        "provisioning",
        STATE,
        STATEFUL,
        STATUS,
        "Five immutable dispatch keys own bounded ACS readiness observations.",
    ),
    "app.tasks.ont_commissioning.cleanup_commissioned_ont": _c(
        "provisioning",
        STATE,
        STATEFUL,
        STATUS,
        "Locked identity and assignment revalidation gates every cleanup attempt.",
    ),
    "app.tasks.ont_commissioning.reconcile_intents": _c(
        "provisioning",
        SWEEP,
        IDEMP,
        STATUS,
        "Permanent convergence pass converts assignments and stages expired cleanup.",
    ),
    "app.tasks.ont_reconcile.run_ont_reconcile_sweep": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.ont_reconcile.alert_overdue_reconcile_holds": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Pure read: reports reconciliation holds past their review date and "
        "releases nothing, so re-running is free. Scheduled independently of "
        "network.ont_reconcile because that control is typically disabled while "
        "holds are in force -- gating the alarm on it would silence it exactly "
        "when the holds are active and least supervised.",
    ),
    "app.tasks.ont_reconcile.reconcile_huawei_ont": _c(
        "network",
        STATE,
        STATEFUL,
        STATUS,
        "Durable parent/child operations expose failure; the dispatch outbox gates "
        "duplicate execution and a new operation owns later convergence retries.",
    ),
    "app.tasks.ont_runtime_status.dispatch_huawei_ont_status": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.ont_runtime_status.refresh_huawei_olt_status": _c(
        "network", AUTORETRY, IDEMP, STATUS
    ),
    "app.tasks.ont_runtime_status.refresh_single_ont_status": _c(
        "network",
        MANUAL,
        STATEFUL,
        REDRIVE,
        "The dispatch outbox gates execution; eligible failures are redriven through "
        "the reviewed network operation recovery owner.",
    ),
    "app.tasks.ont_signal_observations.record_ont_observations": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.payment_reconciliation.reconcile_topups": _c(
        "billing", STATE, GUARDED, HEALTH
    ),
    "app.tasks.durable_timers.fire_due_durable_timers": _c(
        "operations",
        SWEEP,
        IDEMP,
        STATUS,
        "Emits due durable-timer triggers through the timer owner's fire "
        "command; each firing is generation-stamped, so consumers reject "
        "stale deliveries and re-runs are exact no-ops.",
    ),
    "app.tasks.operational_escalations.dispatch_operational_escalation_deliveries": _c(
        "operations",
        SWEEP,
        IDEMP,
        STATUS,
        "Dispatches pending operational escalation deliveries through their "
        "configured channels; terminal and over-retry rows are skipped on re-run.",
    ),
    "app.tasks.profile_sync.execute_due_profile_sync_tasks": _c(
        "network", STATE, STATEFUL, STATUS
    ),
    "app.tasks.provisioning.reap_stale_provisioning_runs": _c(
        "provisioning", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.provisioning.retry_pending_compensation_failures": _c(
        "provisioning",
        STATE,
        STATEFUL,
        REDRIVE,
        "Permanent bounded redrive for durable failed compensations; feature and "
        "module controls cannot strand rollback obligations.",
    ),
    "app.tasks.provisioning.run_bulk_activation_job": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.provisioning.run_service_migration_job": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.quotes.reconcile_quote_mirror": _c("crm", SWEEP, IDEMP, HEALTH),
    "app.tasks.quotes.refresh_quote_mirror_for_subscriber": _c(
        "crm",
        NONE,
        IDEMP,
        LOG,
        "Best-effort on-view refresh; periodic reconcile backs it.",
    ),
    "app.tasks.radius.audit_ip_consistency": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.audit_suspension_enforcement": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.connectivity_shadow_audit": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.reap_radacct_ghosts": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.reconcile_active_sessions": _c(
        "radius",
        SWEEP,
        IDEMP,
        HEALTH,
        "Permanent rebuild of the live-session projection from external accounting.",
    ),
    "app.tasks.radius.run_enforcement_reconciler": _c(
        "radius",
        STATE,
        GUARDED,
        STATUS,
        "Mandatory owner-driven recovery loop; isolates account errors, caps "
        "disconnects, requests one idempotent projection refresh, and records "
        "a degraded outcome until desired and observed access converge.",
    ),
    "app.tasks.radius.run_radius_sync_job": _c("radius", SWEEP, IDEMP, STATUS),
    "app.tasks.radius_population.refresh_radius_from_subs": _c(
        "radius", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.radius_population.sync_device_login": _c(
        "radius",
        SWEEP,
        IDEMP,
        STATUS,
        "Permanent security projection repair from active staff device-login state.",
    ),
    "app.tasks.referrals.reconcile_referral_mirror": _c(
        "referrals", NONE, IDEMP, LOG, "Retired CRM mirror task tombstone."
    ),
    "app.tasks.referrals.refresh_referral_mirror_for_subscriber": _c(
        "referrals",
        NONE,
        IDEMP,
        LOG,
        "Retired CRM mirror task tombstone.",
    ),
    "app.tasks.support_tickets.auto_confirm_resolved_tickets": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Closes pending-confirmation tickets only after their grace window; "
        "closed/responded rows are skipped on re-run.",
    ),
    "app.tasks.team_inbox.retry_failed_outbound_messages": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Retries failed native inbox outbound messages up to a retry cap; "
        "already-sent and over-cap rows are skipped on re-run.",
    ),
    "app.tasks.team_inbox.recover_stale_ai_intake": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Routes only expired AI intake waits through the configured fallback; "
        "locked rows already settled by inbound processing or an earlier sweep "
        "are skipped on re-run.",
    ),
    "app.tasks.team_inbox.release_scheduled_replies": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Sends inbox replies whose scheduled time has passed; each released "
        "row is stamped sent_at so a re-run never sends it twice, and one "
        "failed recipient does not hold up the rest of the batch.",
    ),
    "app.tasks.team_inbox.promote_message_media_assets": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Materializes message attachments into inbox media assets; existing "
        "asset rows are reused on re-run.",
    ),
    "app.tasks.team_inbox.auto_resolve_stale_conversations": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Resolves stale conversations only when the latest customer-visible "
        "message does not require an inbound response.",
    ),
    "app.tasks.team_inbox.backfill_conversation_participants": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Projects conversation participants from stored message headers; only "
        "missing endpoints are admitted, so a re-run over an already-projected "
        "conversation changes nothing.",
    ),
    "app.tasks.team_inbox.wake_due_snoozed_conversations": _c(
        "support",
        SWEEP,
        IDEMP,
        STATUS,
        "Clears snoozed_until once the chosen wake time has passed and returns "
        "the conversation to open; recomputed from the wake time alone, so a "
        "re-run changes nothing and a missed run is caught by the next.",
    ),
    "app.tasks.topology_lldp.run_lldp_topology_poll": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.forwarding_control_observations.run_forwarding_control_observation_poll": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Periodic observation refresh; expiring facts are recomputed and a later "
        "beat run repairs a missed or failed collection.",
    ),
    "app.tasks.topology_metrics.export_topology_metrics": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.outage_auto_notify.auto_dispatch_outage_notifications": _c(
        "network",
        SWEEP,
        GUARDED,
        STATUS,
        "Customer-facing send, so it must never be blindly retried: the "
        "persisted dispatch audit is also the debounce source, and a failed "
        "pass rolls back so no boundary is muted by a send that never "
        "happened. The next beat run re-attempts; a single-flight advisory "
        "lock stops two passes double-notifying. Outcomes surface as dispatch "
        "rows and outage_auto_notify_* log events.",
    ),
    "app.tasks.topology_outage.reconcile_detected_outages": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Classifier outage lifecycle debounce; identity dedupes and "
        "every transition is state-guarded, so re-runs are idempotent.",
    ),
    "app.tasks.topology_sync.warm_topology_status": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.infrastructure_polling.run_infrastructure_poll": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Native ping/SNMP reachability sweep; probes only stale devices, so "
        "re-runs converge instead of duplicating work.",
    ),
    "app.tasks.customer_impact_metrics.export_customer_impact_metrics": _c(
        "monitoring",
        SWEEP,
        IDEMP,
        HEALTH,
        "Read-only fleet impact counters recomputed from source each run.",
    ),
    "app.tasks.radius_health.run_radius_health_check": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Read-only RADIUS accounting/enforcement health pass; every run "
        "recomputes from source, so re-runs are idempotent.",
    ),
    "app.tasks.security.run_scheduled_credential_rotation": _c(
        "security",
        SWEEP,
        GUARDED,
        HEALTH,
        "Daily single-flight credential integrity and key lifecycle; OpenBao "
        "retains the previous key through a grace period and reruns converge "
        "straggler ciphertext.",
    ),
    "app.tasks.topology_ufiber_link.run_ufiber_onu_link": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.uisp_ip_backfill.run_uisp_mgmt_ip_backfill": _c(
        "network",
        MANUAL,
        IDEMP,
        LOG,
        "One-shot inventory backfill; re-runs only stamp still-empty mgmt_ips, "
        "so redriving after a failure is safe.",
    ),
    "app.tasks.uisp_control.apply_uisp_intent": _c("network", STATE, STATEFUL, STATUS),
    "app.tasks.uisp_control.reconcile_uisp_config_readback": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Bounded mapped configuration readback; no writes and repeated reads converge.",
    ),
    "app.tasks.topology_uisp.run_uisp_topology_sync": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Scheduled UISP relationship sync; advisory lock prevents overlap.",
    ),
    "app.tasks.unmatched_radio.run_unmatched_radio_review": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Scheduled unmatched-radio review; re-links radios whose MAC now "
        "matches a subscriber and refreshes the residual ops queue.",
    ),
    "app.tasks.tr069.apply_acs_config": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.tr069.apply_saved_ont_service_config": _c(
        "tr069", STATE, STATEFUL, STATUS
    ),
    "app.tasks.tr069.check_device_health": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.cleanup_stale_genieacs_tasks": _c("tr069", SWEEP, IDEMP, LOG),
    "app.tasks.tr069.cleanup_tr069_records": _c("tr069", SWEEP, IDEMP, LOG),
    "app.tasks.tr069.reconcile_command_outcomes": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.tr069.execute_network_operation_job": _c(
        "tr069",
        NONE,
        GUARDED,
        STATUS,
        "A durable dispatch and queued-to-running claim gate one ACS submission. "
        "Ambiguous or interrupted delivery becomes unverified; it is never "
        "automatically replayed.",
    ),
    "app.tasks.tr069.refresh_ont_runtime_data": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.refresh_single_ont_runtime": _c("tr069", MANUAL, IDEMP, STATUS),
    "app.tasks.tr069.scrape_genieacs_metrics": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.setup_genieacs": _c("tr069", MANUAL, GUARDED, STATUS),
    "app.tasks.tr069.sync_all_acs_devices": _c(
        "tr069",
        AUTORETRY,
        IDEMP,
        HEALTH,
        "A complete pass records duration, server counts, and freshness. "
        "Timeouts, unexpected failures, and partial server passes retry after "
        "1m/5m/15m; the periodic schedule remains the recovery backstop.",
    ),
    "app.tasks.tr069.wait_for_ont_bootstrap": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.usage.evaluate_fup_rules": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.usage.import_radius_accounting": _c("usage", SWEEP, PER_ITEM, HEALTH),
    "app.tasks.usage.lift_expired_fup_enforcement": _c(
        "usage",
        SWEEP,
        GUARDED,
        HEALTH,
        "Permanent reset drainage; disabling new usage/FUP decisions cannot leave "
        "expired throttles or blocks applied.",
    ),
    "app.tasks.usage.meter_usage_into_quota": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.usage.notify_expiring_data_bundles": _c("usage", STATE, GUARDED, STATUS),
    "app.tasks.usage.reap_stale_radius_sessions": _c("usage", SWEEP, IDEMP, HEALTH),
    "app.tasks.usage.run_usage_rating": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.vacation_holds.resume_expired_holds": _c(
        "customer", SWEEP, GUARDED, HEALTH
    ),
    "app.tasks.vpn.run_vpn_control_job": _c("network", STATE, STATEFUL, STATUS),
    "app.tasks.vpn.run_vpn_health_scan": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.wireguard.cleanup_connection_logs": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.wireguard.cleanup_expired_tokens": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.wireguard.generate_connection_log_report": _c(
        "network", MANUAL, IDEMP, STATUS
    ),
    "app.tasks.workflow.detect_sla_breaches": _c("workflow", SWEEP, IDEMP, STATUS),
    "router_sync.capture_scheduled_snapshots": _c("router", SWEEP, IDEMP, HEALTH),
    "router_sync.cleanup_idle_tunnels": _c("router", SWEEP, IDEMP, LOG),
    "router_sync.audit_sot_drift": _c("router", SWEEP, IDEMP, HEALTH),
    "router_sync.execute_config_push": _c("router", STATE, STATEFUL, STATUS),
    "router_sync.reconcile_config_push_readback": _c("router", SWEEP, IDEMP, STATUS),
    "router_sync.reconcile_nas_vlan_readback": _c("router", SWEEP, IDEMP, STATUS),
    "router_sync.sync_all_interfaces": _c("router", SWEEP, IDEMP, HEALTH),
    "router_sync.sync_all_system_info": _c("router", SWEEP, IDEMP, HEALTH),
}


def is_first_party_task(task_name: str) -> bool:
    return task_name.startswith("app.tasks.") or task_name.startswith("router_sync.")


def find_missing_task_reliability_contracts(
    registered_task_names: Iterable[str],
) -> list[str]:
    registered = {
        task_name
        for task_name in registered_task_names
        if is_first_party_task(task_name)
    }
    return sorted(registered - set(TASK_RELIABILITY_CONTRACTS))


def find_stale_task_reliability_contracts(
    registered_task_names: Iterable[str],
) -> list[str]:
    registered = {
        task_name
        for task_name in registered_task_names
        if is_first_party_task(task_name)
    }
    return sorted(set(TASK_RELIABILITY_CONTRACTS) - registered)
