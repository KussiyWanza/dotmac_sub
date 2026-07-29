from __future__ import annotations

import importlib

from app.services import sot_relationships


def test_domain_sot_relationships_cover_expected_domains():
    assert sot_relationships.domain_order() == [
        "party_identity",
        "customer_context",
        "financial_access",
        "network",
        "subscriber_sessions",
        "application_sessions",
        "secrets_credentials",
        "notifications_communications",
        "events_webhooks",
        "runtime_infrastructure",
        "observability",
        "workforce_operations",
        "support_operations",
        "ai_advisory",
        "provisioning_operations",
        "feature_control_plane",
        "authorization_control_plane",
        "scheduler_control_plane",
        "network_access_control_plane",
        "service_intent_control_plane",
        "integration_control_plane",
        "ui_list_projection",
        "ui_bulk_actions",
        "ui_display_formatting",
        "ui_action_forms",
        "ui_semantic_presentation",
        "vpn_remote_access",
        "geospatial",
        "sales_referrals",
    ]


def test_domain_sot_relationships_encode_cross_domain_dependencies():
    assert sot_relationships.dependencies_for("customer.accounts") == (
        "access.subscription_lifecycle",
        "events.dispatcher",
    )
    assert sot_relationships.dependencies_for("party.registry") == (
        "auth.subscriber_assignments",
        "auth.permission_gate",
    )
    assert sot_relationships.dependencies_for("party.identity_audit") == (
        "party.registry",
        "sales.service",
        "sales.orders",
        "access.subscription_lifecycle",
        "operations.provisioning_workflow",
        "financial.invoices",
        "financial.payments",
        "support.ticket_lifecycle",
    )
    assert sot_relationships.dependencies_for("party.identity_adjudication") == (
        "party.identity_audit",
        "party.registry",
    )
    assert sot_relationships.dependencies_for("party.identity_backfill_executor") == (
        "party.identity_audit",
        "party.identity_adjudication",
        "party.registry",
    )
    assert sot_relationships.dependencies_for("party.organization_profile_audit") == (
        "party.registry",
    )
    assert sot_relationships.dependencies_for("party.principal_context_audit") == (
        "party.registry",
        "auth.subscriber_assignments",
        "auth.permission_gate",
    )
    assert sot_relationships.dependencies_for("party.contact_inbox_audit") == (
        "party.registry",
        "communications.team_inbox_contact_resolution",
    )
    assert sot_relationships.dependencies_for("communications.campaigns") == (
        "communications.eligibility",
        "communications.intents",
        "communications.team_inbox_campaigns",
    )
    assert sot_relationships.dependencies_for("sales.lead_lifecycle") == (
        "party.registry",
        "communications.campaigns",
    )
    assert sot_relationships.dependencies_for("sales.service") == (
        "sales.lead_lifecycle",
    )
    assert sot_relationships.dependencies_for("sales.orders") == (
        "sales.service",
        "sales.lead_lifecycle",
        "sales.fulfillment",
    )
    assert sot_relationships.dependencies_for("customer.lifecycle_audit") == (
        "party.registry",
        "communications.campaigns",
        "sales.lead_lifecycle",
        "sales.service",
        "sales.orders",
        "sales.fulfillment",
        "operations.service_order_lifecycle",
        "customer.experience_handoff",
        "access.subscription_lifecycle",
        "support.ticket_lifecycle",
    )
    assert sot_relationships.dependencies_for("referrals.program") == (
        "customer.accounts",
        "party.registry",
        "sales.lead_lifecycle",
        "access.subscription_lifecycle",
        "financial.credit_notes",
        "control.settings_spec",
        "events.dispatcher",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for("referrals.account_conversion") == (
        "customer.accounts",
        "party.registry",
        "sales.lead_lifecycle",
        "referrals.program",
        "auth.token_signing",
        "control.settings_spec",
        "events.dispatcher",
        "observability.audit_log",
    )
    lead_origin = sot_relationships.owning_service_for(
        "immutable structured Lead origin capture"
    )
    assert lead_origin is not None
    assert lead_origin.name == "sales.lead_lifecycle"
    lifecycle_audit = sot_relationships.owning_service_for(
        "PII-free customer lifecycle link convergence report"
    )
    assert lifecycle_audit is not None
    assert lifecycle_audit.name == "customer.lifecycle_audit"
    referral_audit = sot_relationships.owning_service_for(
        "Party-first referral capture and conversion debt classification"
    )
    assert referral_audit is not None
    assert referral_audit.name == "customer.lifecycle_audit"
    referral_attachment = sot_relationships.owning_service_for(
        "Referral Subscriber attachment record"
    )
    assert referral_attachment is not None
    assert referral_attachment.name == "referrals.program"
    account_orchestration = sot_relationships.owning_service_for(
        "atomic referral account creation and adjudication orchestration"
    )
    assert account_orchestration is not None
    assert account_orchestration.name == "referrals.account_conversion"
    public_signup_context = sot_relationships.owning_service_for(
        "stable Referral Party Lead conversion context validation"
    )
    assert public_signup_context is not None
    assert public_signup_context.name == "referrals.account_conversion"
    token_envelope = sot_relationships.owning_service_for(
        "cryptographic signing and verification of typed capability envelopes"
    )
    assert token_envelope is not None
    assert token_envelope.name == "auth.token_signing"
    ephemeral_materialization = sot_relationships.owning_service_for(
        "just-in-time sensitive message materialization orchestration"
    )
    assert ephemeral_materialization is not None
    assert ephemeral_materialization.name == "communications.ephemeral_actions"
    assert sot_relationships.dependencies_for(
        "auth.customer_credential_enrollment"
    ) == (
        "auth.token_signing",
        "communications.intents",
        "customer.accounts",
        "referrals.account_conversion",
        "communications.ephemeral_actions",
        "control.settings_spec",
        "events.dispatcher",
        "observability.audit_log",
    )
    cleanup_worklist = sot_relationships.owning_service_for(
        "subscriber cleanup worklist contract"
    )
    assert cleanup_worklist is not None
    assert cleanup_worklist.name == "party.identity_audit"
    reseller_contract = sot_relationships.owning_service_for(
        "reseller versus partner role contract"
    )
    assert reseller_contract is not None
    assert reseller_contract.name == "party.registry"
    contact_identity = sot_relationships.owning_service_for(
        "provider-scoped immutable social contact identity"
    )
    assert contact_identity is not None
    assert contact_identity.name == "party.registry"
    subscriber_binding = sot_relationships.owning_service_for(
        "subscriber-account canonical party binding"
    )
    assert subscriber_binding is not None
    assert subscriber_binding.name == "party.registry"
    backfill_plan = sot_relationships.owning_service_for(
        "Party backfill dry-run plan digest"
    )
    assert backfill_plan is not None
    assert backfill_plan.name == "party.identity_adjudication"
    backfill_receipt = sot_relationships.owning_service_for(
        "Party identity backfill execution receipt"
    )
    assert backfill_receipt is not None
    assert backfill_receipt.name == "party.identity_backfill_executor"
    organization_profile_binding = sot_relationships.owning_service_for(
        "organization role-profile canonical party binding"
    )
    assert organization_profile_binding is not None
    assert organization_profile_binding.name == "party.registry"
    vendor_bridge_audit = sot_relationships.owning_service_for(
        "Vendor and FieldVendor bridge debt classification"
    )
    assert vendor_bridge_audit is not None
    assert vendor_bridge_audit.name == "party.organization_profile_audit"
    principal_binding = sot_relationships.owning_service_for(
        "SystemUser principal to Person Party binding"
    )
    assert principal_binding is not None
    assert principal_binding.name == "party.registry"
    vendor_user_bridge_audit = sot_relationships.owning_service_for(
        "FieldVendorUser vendor context debt classification"
    )
    assert vendor_user_bridge_audit is not None
    assert vendor_user_bridge_audit.name == "party.principal_context_audit"
    contact_projection = sot_relationships.owning_service_for(
        "reviewed SubscriberContact source-field contact-point projection"
    )
    assert contact_projection is not None
    assert contact_projection.name == "party.registry"
    contact_inbox_audit = sot_relationships.owning_service_for(
        "Team Inbox canonical contact-point projection debt report"
    )
    assert contact_inbox_audit is not None
    assert contact_inbox_audit.name == "party.contact_inbox_audit"
    assert sot_relationships.dependencies_for("network.outage_impact") == (
        "network.access_path",
        "network.forwarding_topology",
    )
    assert sot_relationships.dependencies_for("network.fiber_topology") == (
        "network.identity",
        "gis.spatial_sync",
        "network.fiber_source_staging",
    )
    assert sot_relationships.dependencies_for("network.fiber_source_staging") == (
        "gis.spatial_sync",
    )
    assert sot_relationships.dependencies_for("network.fiber_plant_integrity") == (
        "network.fiber_topology",
    )
    assert sot_relationships.dependencies_for("network.splitter_inventory") == (
        "network.fiber_plant_integrity",
    )
    assert sot_relationships.dependencies_for("network.fiber_physical_continuity") == (
        "network.fiber_topology",
        "network.fiber_plant_integrity",
    )
    assert sot_relationships.dependencies_for("network.fiber_asset_changes") == (
        "network.fiber_topology",
        "network.fiber_plant_integrity",
        "network.splitter_inventory",
        "network.fiber_support_structures",
        "network.fiber_physical_continuity",
    )
    assert sot_relationships.dependencies_for("network.fiber_support_structures") == (
        "network.fiber_topology",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for("network.fiber_identity_decisions") == (
        "network.fiber_topology",
        "network.fiber_asset_changes",
        "network.fiber_support_structures",
    )
    assert sot_relationships.dependencies_for("network.fiber_identity_review") == (
        "network.fiber_identity_decisions",
    )
    assert sot_relationships.dependencies_for("network.fiber_field_observations") == (
        "network.fiber_source_staging",
        "operations.work_orders",
        "network.fiber_field_verification_job_scope",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_field_verification_worklist"
    ) == (
        "network.fiber_source_staging",
        "network.fiber_field_observations",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_field_verification_jobs"
    ) == (
        "network.fiber_field_verification_worklist",
        "network.fiber_field_verification_job_scope",
        "operations.work_order_commands",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_field_verification_map"
    ) == (
        "network.fiber_source_staging",
        "network.fiber_field_verification_worklist",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_work_order_evidence_map"
    ) == (
        "operations.work_orders",
        "network.fiber_field_observations",
        "network.fiber_field_verification_map",
    )
    assert sot_relationships.dependencies_for("network.fiber_identity_coverage") == (
        "network.fiber_source_staging",
        "network.fiber_asset_changes",
        "network.fiber_field_observations",
        "network.fiber_identity_decisions",
        "network.fiber_identity_review",
        "network.fiber_support_structures",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_connectivity_decisions"
    ) == (
        "network.fiber_topology",
        "network.fiber_asset_changes",
        "network.fiber_identity_decisions",
    )
    assert sot_relationships.dependencies_for("network.fiber_connectivity_review") == (
        "network.fiber_connectivity_decisions",
    )
    assert sot_relationships.dependencies_for(
        "network.fiber_connectivity_coverage"
    ) == (
        "network.fiber_source_staging",
        "network.fiber_asset_changes",
        "network.fiber_field_observations",
        "network.fiber_connectivity_decisions",
        "network.fiber_connectivity_review",
    )
    assert sot_relationships.dependencies_for("network.fiber_access_attachments") == (
        "network.fiber_topology",
        "network.fiber_connectivity_decisions",
        "network.ont_assignment_commands",
        "network.ont_assignment_identity",
    )
    assert sot_relationships.dependencies_for("network.ont_topology_observations") == (
        "network.fiber_topology",
    )
    assert sot_relationships.dependencies_for("network.ont_assignment_identity") == (
        "network.fiber_topology",
        "network.ont_topology_observations",
        "network.ont_assignment_commands",
    )
    assert sot_relationships.dependencies_for("network.ont_assignment_commands") == (
        "network.identity",
        "network.ont_topology_observations",
    )
    assert sot_relationships.dependencies_for("network.ont_assignment_cutover") == (
        "network.ont_assignment_commands",
        "network.ont_assignment_identity",
    )
    assert sot_relationships.dependencies_for(
        "network.ont_assignment_cutover_batches"
    ) == (
        "network.ont_assignment_cutover",
        "network.ont_assignment_identity",
    )
    assert sot_relationships.dependencies_for(
        "network.ont_assignment_cutover_verification"
    ) == (
        "network.ont_assignment_cutover",
        "network.ont_assignment_cutover_batches",
        "network.ont_assignment_identity",
    )
    assert sot_relationships.dependencies_for(
        "network.ont_assignment_cutover_coverage"
    ) == (
        "network.ont_assignment_cutover",
        "network.ont_assignment_cutover_batches",
        "network.ont_assignment_cutover_verification",
        "network.ont_assignment_identity",
    )
    assert sot_relationships.dependencies_for(
        "network.ont_assignment_constraint_authorization"
    ) == ("network.ont_assignment_cutover_coverage",)
    assert sot_relationships.dependencies_for("network.ont_inventory_release") == (
        "network.ont_assignment_commands",
        "network.ont_assignment_identity",
        "network.ont_topology_observations",
    )
    assert sot_relationships.dependencies_for("network.access_path") == (
        "network.identity",
        "network.fiber_topology",
        "network.ont_assignment_commands",
        "network.ont_assignment_identity",
        "network.fiber_access_attachments",
        "network.fiber_physical_continuity",
        "network.forwarding_topology",
    )
    assert sot_relationships.dependencies_for("network.outage_lifecycle") == (
        "network.outage_impact",
        "events.dispatcher",
        "events.owner_outputs",
        "operations.sla_escalation",
    )
    assert sot_relationships.dependencies_for("network.device_groups") == (
        "network.identity",
    )
    assert sot_relationships.dependencies_for("network.routeros_sot") == (
        "network.identity",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("network.monitoring_inventory") == (
        "network.identity",
    )
    assert sot_relationships.dependencies_for("network.ont_status_refresh") == (
        "network.device_state",
        "network.ont_runtime_status",
    )
    assert sot_relationships.dependencies_for("network.ont_runtime_status") == (
        "runtime.infrastructure_polling",
    )
    assert sot_relationships.dependencies_for("financial.dunning") == (
        "financial.access_resolution",
        "financial.ledger",
        "financial.payment_arrangements",
        "financial.billing_health",
        "financial.prepaid_enforcement_state",
        "access.subscription_lifecycle",
        "access.walled_garden_policy",
    )
    assert sot_relationships.dependencies_for("access.subscription_lifecycle") == (
        "events.dispatcher",
        "financial.prepaid_enforcement_state",
    )
    assert sot_relationships.dependencies_for("financial.access_resolution") == (
        "financial.billing_profile",
        "financial.prepaid_currency",
        "financial.prepaid_threshold",
        "customer.financial_position",
        "access.subscription_lifecycle",
        "access.walled_garden_policy",
    )
    assert sot_relationships.dependencies_for("customer.financial_position") == (
        "financial.credit_notes",
        "financial.invoices",
        "financial.ledger",
        "financial.payments",
        "financial.prepaid_funding_reconstruction",
    )
    assert sot_relationships.dependencies_for("financial.prepaid_enforcement") == (
        "access.subscription_lifecycle",
        "communications.customer_policy",
        "control.settings_spec",
        "customer.accounts",
        "financial.prepaid_funding_reconstruction",
        "financial.access_resolution",
        "financial.billing_profile",
        "financial.dunning",
        "financial.prepaid_currency",
        "financial.prepaid_enforcement_state",
        "financial.prepaid_threshold",
        "financial.grace_policy",
        "service_intent.catalog_policy",
    )
    assert sot_relationships.dependencies_for("financial.billing_scheduled") == (
        "financial.ledger",
        "financial.access_resolution",
        "financial.billing_health",
    )
    financial_services = sot_relationships.service_names_for_domain("financial_access")
    assert "financial.payment_arrangements" in financial_services
    assert "financial.billing_health" in financial_services
    assert "financial.prepaid_funding_reconstruction" in financial_services
    assert sot_relationships.dependencies_for("financial.collections_scheduled") == (
        "financial.dunning",
        "financial.access_resolution",
        "financial.prepaid_enforcement",
        "financial.prepaid_enforcement_state",
    )
    assert sot_relationships.dependencies_for("financial.payment_webhooks") == (
        "integration.inbox",
        "financial.account_credit_deposits",
        "financial.payment_provider_events",
        "financial.topup_intents",
    )
    assert sot_relationships.dependencies_for("financial.payment_provider_events") == (
        "events.dispatcher",
        "financial.consolidated_payments",
        "financial.invoices",
        "financial.payments",
        "financial.payment_routing",
        "financial.provider_payment_settlements",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for(
        "financial.provider_payment_settlements"
    ) == (
        "financial.payments",
        "financial.invoices",
        "financial.prepaid_service_renewals",
    )
    assert sot_relationships.dependencies_for("financial.account_credit_deposits") == (
        "customer.accounts",
        "events.dispatcher",
        "financial.account_credit_applications",
        "financial.prepaid_service_renewals",
        "financial.access_resolution",
        "financial.invoices",
        "financial.payments",
        "financial.topup_intents",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for("financial.prepaid_service_renewals") == (
        "financial.account_adjustments",
        "financial.invoices",
        "financial.payments",
        "financial.prepaid_funding_reconstruction",
        "financial.subscription_billing_grants",
        "financial.subscription_billing_treatments",
        "events.dispatcher",
    )
    assert sot_relationships.dependencies_for("financial.payment_reconciliation") == (
        "control.settings_spec",
        "integration.runtime",
        "financial.account_credit_deposits",
        "financial.payments",
        "financial.payment_provider_events",
        "financial.topup_intents",
    )
    assert sot_relationships.dependencies_for("customer.service_status") == (
        "financial.access_resolution",
        "customer.financial_position",
        "financial.grace_policy",
    )
    assert sot_relationships.dependencies_for("customer.profile_commands") == (
        "customer.identity_scope",
    )
    assert sot_relationships.dependencies_for("customer.name_remediation") == (
        "customer.profile_commands",
    )
    business_conversion = sot_relationships.owning_service_for(
        "person-to-business customer conversion"
    )
    assert business_conversion is not None
    assert business_conversion.name == "customer.profile_commands"
    name_remediation = sot_relationships.owning_service_for(
        "evidence-bound legacy Subscriber name repair"
    )
    assert name_remediation is not None
    assert name_remediation.name == "customer.name_repairs"
    assert name_remediation.contract is not None
    assert sot_relationships.dependencies_for("customer.usage_summary") == (
        "sessions.radius_reconciliation",
    )
    assert sot_relationships.dependencies_for("financial.prepaid_plan_change") == (
        "financial.account_adjustments",
        "financial.credit_notes",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.account_adjustments") == (
        "financial.ledger",
        "customer.financial_position",
        "customer.accounts",
        "control.settings_spec",
        "events.dispatcher",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for(
        "financial.import_payment_batch_reversals"
    ) == (
        "financial.payments",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("financial.addon_purchases") == (
        "financial.account_adjustments",
        "customer.financial_position",
    )
    assert sot_relationships.dependencies_for("ui.customer_list_projection") == (
        "ui.list_contracts",
    )
    assert sot_relationships.dependencies_for("ui.referral_list_projection") == (
        "ui.list_contracts",
        "ui.projection_contracts",
        "referrals.program",
    )
    assert sot_relationships.dependencies_for("ui.projection_contracts") == (
        "ui.status_presentation",
    )
    customer_api_mapping = sot_relationships.owning_service_for(
        "legacy customer offset API compatibility mapping"
    )
    assert customer_api_mapping is not None
    assert customer_api_mapping.name == "ui.customer_list_projection"
    assert sot_relationships.dependencies_for("ui.subscriber_list_projection") == (
        "ui.list_contracts",
    )
    assert sot_relationships.dependencies_for("ui.invoice_list_projection") == (
        "ui.list_contracts",
        "financial.invoices",
    )
    assert sot_relationships.dependencies_for("ui.support_ticket_list_projection") == (
        "ui.list_contracts",
        "support.ticket_lifecycle",
        "support.ticket_configuration",
    )
    assert sot_relationships.dependencies_for("ui.bulk_action_contracts") == (
        "ui.list_contracts",
    )
    assert sot_relationships.dependencies_for("ui.customer_bulk_action_projection") == (
        "ui.bulk_action_contracts",
        "ui.customer_list_projection",
    )
    assert sot_relationships.dependencies_for("ui.invoice_bulk_action_projection") == (
        "ui.bulk_action_contracts",
        "ui.invoice_list_projection",
        "financial.invoices",
    )
    assert sot_relationships.dependencies_for("support.ticket_bulk_commands") == (
        "support.ticket_lifecycle",
        "support.ticket_configuration",
        "ui.bulk_action_contracts",
    )
    assert sot_relationships.dependencies_for(
        "ui.support_ticket_bulk_action_projection"
    ) == (
        "ui.bulk_action_contracts",
        "ui.support_ticket_list_projection",
        "support.ticket_bulk_commands",
    )
    assert sot_relationships.dependencies_for("ui.status_presentation") == (
        "customer.service_status",
        "financial.invoices",
        "financial.payments",
        "network.device_state",
        "network.connection_health",
        "network.outage_lifecycle",
        "support.ticket_lifecycle",
        "operations.work_order_status",
        "operations.vendor_project_lifecycle",
        "operations.vendor_project_workspace",
        "operations.vendor_material_release",
        "operations.vendor_advances",
        "integration.dotmac_erp_payables_adapter",
    )
    assert sot_relationships.dependencies_for("operations.material_dependencies") == (
        "control.settings_spec",
        "events.dispatcher",
        "operations.work_orders",
        "operations.work_order_status",
    )
    assert sot_relationships.dependencies_for(
        "integration.dotmac_erp_payables_adapter"
    ) == ("integration.backoffice_adapter",)
    assert sot_relationships.dependencies_for(
        "integration.dotmac_erp_material_support_adapter"
    ) == (
        "integration.backoffice_adapter",
        "operations.material_dependencies",
    )
    subscriber_api_mapping = sot_relationships.owning_service_for(
        "legacy subscriber offset API compatibility mapping"
    )
    assert subscriber_api_mapping is not None
    assert subscriber_api_mapping.name == "ui.subscriber_list_projection"
    assert sot_relationships.dependencies_for(
        "communications.notification_service"
    ) == ("communications.channel_policy", "communications.customer_policy")
    assert sot_relationships.dependencies_for("communications.customer_read_state") == (
        "customer.identity_scope",
        "communications.customer_policy",
        "communications.notification_service",
    )
    assert sot_relationships.dependencies_for("secrets.rotation") == (
        "secrets.reference_store",
        "secrets.credential_integrity",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("secrets.credential_integrity") == (
        "secrets.access_credential_format",
        "secrets.credential_crypto",
        "observability.recording",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("secrets.credential_recovery") == (
        "secrets.credential_integrity",
        "network.identity",
        "network.radius_sessions",
        "access.radius_state",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("communications.team_inbox_commands") == (
        "auth.permission_gate",
        "communications.team_inbox_threads",
        "communications.team_inbox_contact_resolution",
        "communications.team_inbox_routing",
        "communications.team_inbox_outbound_intents",
        "communications.team_inbox_operator_state",
    )
    assert sot_relationships.dependencies_for("sessions.enforcement") == (
        "financial.access_resolution",
        "sessions.radius_resolution",
    )
    assert sot_relationships.dependencies_for("sessions.radius_accounting_health") == (
        "control.domain_settings",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("runtime.infrastructure_polling") == (
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("operations.project_lifecycle") == (
        "auth.permission_gate",
        "events.dispatcher",
        "communications.staff_notifications",
        "operations.work_order_commands",
    )
    assert sot_relationships.dependencies_for("operations.work_order_commands") == (
        "customer.identity_scope",
        "operations.work_order_status",
        "observability.audit_log",
    )
    assert sot_relationships.dependencies_for("operations.field_completion") == (
        "operations.work_orders",
        "operations.work_order_status",
        "control.domain_settings",
    )
    assert sot_relationships.dependencies_for("network.nas_lifecycle") == (
        "network.identity",
        "network.access_path",
        "network.radius_sessions",
        "network.nas_inventory",
        "service_intent.subscription_nas_assignment",
        "access.radius_state",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert sot_relationships.dependencies_for("network.nas_access_path_evidence") == (
        "network.radius_sessions",
        "network.nas_lifecycle",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("control.feature_registry") == (
        "control.module_manager",
        "control.domain_settings",
    )
    assert sot_relationships.dependencies_for("scheduler.registry") == (
        "control.feature_registry",
        "control.settings_spec",
        "runtime.db_sessions",
    )
    assert sot_relationships.dependencies_for("access.radius_state") == (
        "access.subscription_lifecycle",
        "access.walled_garden_policy",
        "financial.access_resolution",
    )
    assert sot_relationships.dependencies_for("access.radius_projection") == (
        "access.subscription_lifecycle",
        "access.radius_state",
        "access.radius_reject",
        "access.radius_target_registry",
        "control.settings_spec",
    )
    assert sot_relationships.dependencies_for("communications.intents") == (
        "communications.channel_policy",
        "communications.customer_policy",
        "communications.eligibility",
        "communications.notification_service",
    )
    assert sot_relationships.dependencies_for("sessions.radius_resolution") == (
        "sessions.radius_reconciliation",
        "network.identity",
    )
    assert sot_relationships.dependencies_for(
        "service_intent.catalog_billing_governance"
    ) == (
        "service_intent.catalog_validation",
        "auth.permission_gate",
        "observability.recording",
    )


def test_domain_sot_relationships_resolve_owning_service_by_concern():
    service = sot_relationships.owning_service_for("RADIUS access decision")

    assert service is not None
    assert service.name == "financial.access_resolution"
    assert service.module == "app.services.access_resolution"

    presentation_service = sot_relationships.owning_service_for(
        "account status labels, semantic tones, and icon keys"
    )

    assert presentation_service is not None
    assert presentation_service.name == "ui.status_presentation"
    assert presentation_service.module == "app.services.status_presentation"

    work_order_status = sot_relationships.owning_service_for(
        "persisted work-order status vocabulary"
    )
    assert work_order_status is not None
    assert work_order_status.name == "operations.work_order_status"

    work_order_commands = sot_relationships.owning_service_for(
        "work-order assignment decisions and projection"
    )
    assert work_order_commands is not None
    assert work_order_commands.name == "operations.work_order_commands"
    assert work_order_commands.module == "app.services.work_order_commands"

    work_order_presentation = sot_relationships.owning_service_for(
        "field work-order status labels, semantic tones, and icon keys"
    )
    assert work_order_presentation is not None
    assert work_order_presentation.name == "ui.status_presentation"

    fiber_evidence_presentation = sot_relationships.owning_service_for(
        "work-order evidence and geometry presentation semantics"
    )
    assert fiber_evidence_presentation is not None
    assert fiber_evidence_presentation.name == "network.fiber_work_order_evidence_map"

    ticket_lifecycle = sot_relationships.owning_service_for(
        "guarded ticket status transitions"
    )
    assert ticket_lifecycle is not None
    assert ticket_lifecycle.name == "support.ticket_lifecycle"
    assert ticket_lifecycle.module == "app.services.support"

    ticket_presentation = sot_relationships.owning_service_for(
        "support-ticket status labels, semantic tones, and icon keys"
    )
    assert ticket_presentation is not None
    assert ticket_presentation.name == "ui.status_presentation"

    invoice_presentation = sot_relationships.owning_service_for(
        "invoice status labels, semantic tones, and icon keys"
    )
    assert invoice_presentation is not None
    assert invoice_presentation.name == "ui.status_presentation"

    invoice_lifecycle = sot_relationships.owning_service_for(
        "invoice status transitions"
    )
    assert invoice_lifecycle is not None
    assert invoice_lifecycle.name == "financial.invoices"

    automation_invoice_owner = sot_relationships.owning_service_for(
        "automation invoice creation and draft issuance"
    )
    assert automation_invoice_owner is not None
    assert automation_invoice_owner.name == "financial.invoices"

    automation_line_owner = sot_relationships.owning_service_for(
        "automation invoice-line construction and source-fact replay"
    )
    assert automation_line_owner is not None
    assert automation_line_owner.name == "financial.invoices"

    usage_invoice_owner = sot_relationships.owning_service_for(
        "usage-charge invoice and invoice-line construction"
    )
    assert usage_invoice_owner is not None
    assert usage_invoice_owner.name == "financial.invoices"

    overdue_invoice_owner = sot_relationships.owning_service_for(
        "overdue invoice state and observation event"
    )
    assert overdue_invoice_owner is not None
    assert overdue_invoice_owner.name == "financial.invoices"

    payment_presentation = sot_relationships.owning_service_for(
        "payment status labels, semantic tones, and icon keys"
    )
    assert payment_presentation is not None
    assert payment_presentation.name == "ui.status_presentation"

    payment_lifecycle = sot_relationships.owning_service_for(
        "payment document lifecycle"
    )
    assert payment_lifecycle is not None
    assert payment_lifecycle.name == "financial.payments"

    settlement_owner = sot_relationships.owning_service_for(
        "confirmed payment settlement preview and evidence"
    )
    assert settlement_owner is not None
    assert settlement_owner.name == "financial.payments"

    consolidated_settlement_owner = sot_relationships.owning_service_for(
        "consolidated payment settlement preview and confirmation"
    )
    assert consolidated_settlement_owner is not None
    assert consolidated_settlement_owner.name == "financial.consolidated_payments"

    consolidated_reconciliation_owner = sot_relationships.owning_service_for(
        "historical consolidated settlement evidence reconciliation"
    )
    assert consolidated_reconciliation_owner is not None
    assert consolidated_reconciliation_owner.name == "financial.consolidated_payments"

    consolidated_provenance_owner = sot_relationships.owning_service_for(
        "exact consolidated settlement cash provenance links"
    )
    assert consolidated_provenance_owner is not None
    assert consolidated_provenance_owner.name == "financial.consolidated_payments"

    consolidated_ledger_owner = sot_relationships.owning_service_for(
        "exact consolidated-credit ledger links"
    )
    assert consolidated_ledger_owner is not None
    assert consolidated_ledger_owner.name == "financial.consolidated_payments"

    consolidated_credit_allocation_owner = sot_relationships.owning_service_for(
        "consolidated-credit allocation preview and confirmation"
    )
    assert consolidated_credit_allocation_owner is not None
    assert (
        consolidated_credit_allocation_owner.name == "financial.consolidated_payments"
    )

    consolidated_credit_evidence_owner = sot_relationships.owning_service_for(
        "exact source-credit consumption and subscriber-ledger links"
    )
    assert consolidated_credit_evidence_owner is not None
    assert consolidated_credit_evidence_owner.name == "financial.consolidated_payments"

    consolidated_credit_reconciliation_owner = sot_relationships.owning_service_for(
        "historical consolidated-credit consumption reconciliation"
    )
    assert consolidated_credit_reconciliation_owner is not None
    assert (
        consolidated_credit_reconciliation_owner.name
        == "financial.consolidated_payments"
    )

    consolidated_projection_repair_owner = sot_relationships.owning_service_for(
        "exact billing-account projection-debit repair evidence"
    )
    assert consolidated_projection_repair_owner is not None
    assert (
        consolidated_projection_repair_owner.name == "financial.consolidated_payments"
    )

    consolidated_refund_owner = sot_relationships.owning_service_for(
        "billing-account refund confirmation and exact ledger evidence"
    )
    assert consolidated_refund_owner is not None
    assert consolidated_refund_owner.name == "financial.consolidated_payments"

    consolidated_reversal_owner = sot_relationships.owning_service_for(
        "billing-account reversal confirmation and exact ledger evidence"
    )
    assert consolidated_reversal_owner is not None
    assert consolidated_reversal_owner.name == "financial.consolidated_payments"

    consolidated_return_reconciliation_owner = sot_relationships.owning_service_for(
        "historical consolidated refund/reversal evidence reconciliation"
    )
    assert consolidated_return_reconciliation_owner is not None
    assert (
        consolidated_return_reconciliation_owner.name
        == "financial.consolidated_payments"
    )

    consolidated_return_provenance_owner = sot_relationships.owning_service_for(
        "exact historical consolidated return provenance links"
    )
    assert consolidated_return_provenance_owner is not None
    assert (
        consolidated_return_provenance_owner.name == "financial.consolidated_payments"
    )

    consolidated_return_document_owner = sot_relationships.owning_service_for(
        "historical consolidated return document reconstruction"
    )
    assert consolidated_return_document_owner is not None
    assert consolidated_return_document_owner.name == "financial.consolidated_payments"

    consolidated_return_source_owner = sot_relationships.owning_service_for(
        "reviewed historical return source references"
    )
    assert consolidated_return_source_owner is not None
    assert consolidated_return_source_owner.name == "financial.consolidated_payments"

    allocation_owner = sot_relationships.owning_service_for(
        "settled account-credit allocation preview and confirmation"
    )
    assert allocation_owner is not None
    assert allocation_owner.name == "financial.payments"

    native_credit_reconciliation_owner = sot_relationships.owning_service_for(
        "native unallocated-credit reconciliation transactions"
    )
    assert native_credit_reconciliation_owner is not None
    assert native_credit_reconciliation_owner.name == "financial.payments"

    refund_owner = sot_relationships.owning_service_for(
        "payment refund confirmation and exact ledger evidence"
    )
    assert refund_owner is not None
    assert refund_owner.name == "financial.payments"

    reversal_owner = sot_relationships.owning_service_for(
        "payment reversal confirmation and exact ledger evidence"
    )
    assert reversal_owner is not None
    assert reversal_owner.name == "financial.payments"

    provider_reversal_owner = sot_relationships.owning_service_for(
        "normalized provider reversal evidence"
    )
    assert provider_reversal_owner is not None
    assert provider_reversal_owner.name == "financial.payments"

    batch_reversal_owner = sot_relationships.owning_service_for(
        "exact import-row-to-settlement-to-reversal ledger links"
    )
    assert batch_reversal_owner is not None
    assert batch_reversal_owner.name == "financial.import_payment_batch_reversals"

    account_adjustment_owner = sot_relationships.owning_service_for(
        "exact account-adjustment ledger links"
    )
    assert account_adjustment_owner is not None
    assert account_adjustment_owner.name == "financial.account_adjustments"

    addon_purchase_owner = sot_relationships.owning_service_for(
        "exact add-on entitlement-to-adjustment link"
    )
    assert addon_purchase_owner is not None
    assert addon_purchase_owner.name == "financial.addon_purchases"

    semantic_palette = sot_relationships.owning_service_for(
        "brand primary, secondary, and semantic UI color roles"
    )
    assert semantic_palette is not None
    assert semantic_palette.name == "customer.branding"

    outage_lifecycle = sot_relationships.owning_service_for(
        "persisted outage incident status vocabulary"
    )
    assert outage_lifecycle is not None
    assert outage_lifecycle.name == "network.outage_lifecycle"

    outage_presentation = sot_relationships.owning_service_for(
        "outage incident status labels, semantic tones, and icon keys"
    )
    assert outage_presentation is not None
    assert outage_presentation.name == "ui.status_presentation"

    device_state = sot_relationships.owning_service_for(
        "device operational status vocabulary and reason classification"
    )
    assert device_state is not None
    assert device_state.name == "network.device_state"

    device_presentation = sot_relationships.owning_service_for(
        "device operational status labels, semantic tones, and icon keys"
    )
    assert device_presentation is not None
    assert device_presentation.name == "ui.status_presentation"

    connection_health = sot_relationships.owning_service_for(
        "customer-safe connection health vocabulary"
    )
    assert connection_health is not None
    assert connection_health.name == "network.connection_health"

    connection_presentation = sot_relationships.owning_service_for(
        "customer connection health labels, semantic tones, and icon keys"
    )
    assert connection_presentation is not None
    assert connection_presentation.name == "ui.status_presentation"

    control_service = sot_relationships.owning_service_for(
        "module/feature/safety control resolution"
    )

    assert control_service is not None
    assert control_service.name == "control.feature_registry"

    cadence_owner = sot_relationships.owning_service_for("subscription billing cadence")
    assert cadence_owner is not None
    assert cadence_owner.name == "service_intent.subscription_billing_cadence"
    assert cadence_owner.module == "app.services.catalog.subscriptions"

    project_service = sot_relationships.owning_service_for(
        "Project and ProjectTask identity and lifecycle"
    )

    assert project_service is not None
    assert project_service.name == "operations.project_lifecycle"
    assert project_service.module == "app.services.projects"

    completion_service = sot_relationships.owning_service_for(
        "field completion evidence requirements"
    )

    assert completion_service is not None
    assert completion_service.name == "operations.field_completion"
    assert completion_service.module == "app.services.field.transitions"

    action_service = sot_relationships.owning_service_for(
        "payment-restores-service claims"
    )

    assert action_service is not None
    assert action_service.name == "customer.service_status"
    assert action_service.module == "app.services.service_status"

    usage_service = sot_relationships.owning_service_for(
        "customer usage headline totals"
    )

    assert usage_service is not None
    assert usage_service.name == "customer.usage_summary"
    assert usage_service.module == "app.services.usage_summary"

    read_state_service = sot_relationships.owning_service_for(
        "customer notification read/unread state"
    )

    assert read_state_service is not None
    assert read_state_service.name == "communications.customer_read_state"
    assert read_state_service.module == "app.services.customer_portal_notifications"

    team_inbox_service = sot_relationships.owning_service_for(
        "operator conversation and collaboration commands"
    )

    assert team_inbox_service is not None
    assert team_inbox_service.name == "communications.team_inbox_commands"
    assert team_inbox_service.module == "app.services.team_inbox_commands"
    contact_resolution_service = sot_relationships.owning_service_for(
        "reviewed contact association and projection repair"
    )
    assert contact_resolution_service is not None
    assert (
        contact_resolution_service.name
        == "communications.team_inbox_contact_resolution"
    )


def test_domain_sot_relationship_modules_are_importable():
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            importlib.import_module(service.module)
