"""Materialize canonical feature controls and retire legacy aliases.

Revision ID: 284_retire_feature_aliases
Revises: 283_canonical_feature_controls

The canonical modules-domain row is the only runtime value source after this
revision.  Existing canonical decisions win.  Otherwise an active legacy row
is converted to a canonical boolean row before the retired row is removed.

``billing.billing_enabled`` is intentionally retained: it is the independent
cross-feature billing master switch, not an editable alias after cutover.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "309_retire_feature_aliases"
down_revision = "308_canonical_feature_controls"
branch_labels = None
depends_on = None

# (canonical control, legacy domain, legacy key, retain legacy row)
_ALIASES = (
    ("billing.invoicing", "billing", "billing_enabled", True),
    ("billing.autopay", "billing", "autopay_enabled", False),
    ("billing.collections", "collections", "dunning_enabled", False),
    ("billing.overdue_marking", "billing", "overdue_check_enabled", False),
    ("billing.arrangements", "collections", "arrangement_check_enabled", False),
    (
        "billing.topup_reconciliation",
        "billing",
        "topup_reconciliation_enabled",
        False,
    ),
    (
        "billing.notifications_hourly",
        "collections",
        "billing_notifications_hourly_enabled",
        False,
    ),
    (
        "collections.prepaid_balance_enforcement",
        "collections",
        "prepaid_balance_enforcement_enabled",
        False,
    ),
    (
        "billing.prepaid_monthly_invoicing",
        "billing",
        "prepaid_monthly_invoicing_enabled",
        False,
    ),
    (
        "billing.direct_bank_transfer",
        "billing",
        "direct_bank_transfer_enabled",
        False,
    ),
    (
        "catalog.subscription_expiration",
        "catalog",
        "subscription_expiration_enabled",
        False,
    ),
    (
        "catalog.vacation_hold_resume",
        "catalog",
        "vacation_hold_auto_resume_enabled",
        False,
    ),
    (
        "customer.services_view",
        "modules",
        "module_customer_services_enabled",
        False,
    ),
    (
        "notifications.queue",
        "notification",
        "notification_queue_enabled",
        False,
    ),
    ("usage.warnings", "usage", "usage_warning_enabled", False),
    (
        "usage.fup_submonthly_rules",
        "usage",
        "fup_submonthly_rules_enabled",
        False,
    ),
    (
        "sessions.radius_accounting_import",
        "usage",
        "radius_accounting_import_enabled",
        False,
    ),
    (
        "sessions.radius_reap_stale",
        "usage",
        "radius_session_reap_enabled",
        False,
    ),
    ("access.radius_coa", "radius", "coa_enabled", False),
    (
        "access.mikrotik_session_kill",
        "network",
        "mikrotik_session_kill_enabled",
        False,
    ),
    (
        "access.mikrotik_api_session_kick",
        "network",
        "mikrotik_api_session_kick_enabled",
        False,
    ),
    (
        "access.address_list_block",
        "network",
        "address_list_block_enabled",
        False,
    ),
    ("vas.wallet", "vas", "enabled", False),
    (
        "provisioning.compensation_retry",
        "provisioning",
        "compensation_retry_enabled",
        False,
    ),
    (
        "network.olt_profile_sync",
        "network",
        "olt_profile_sync_worker_enabled",
        False,
    ),
    ("network.tr069_sync", "network", "tr069_sync_enabled", False),
    (
        "network.tr069_job_execution",
        "network",
        "tr069_job_execution_enabled",
        False,
    ),
    (
        "network.tr069_health_check",
        "network",
        "tr069_health_check_enabled",
        False,
    ),
    ("network.tr069_cleanup", "network", "tr069_cleanup_enabled", False),
    (
        "network.tr069_genieacs_stale_cleanup",
        "network",
        "tr069_genieacs_stale_cleanup_enabled",
        False,
    ),
    (
        "network.tr069_metrics_scrape",
        "network",
        "tr069_metrics_scrape_enabled",
        False,
    ),
    (
        "network.tr069_ont_runtime_refresh",
        "network",
        "tr069_ont_runtime_refresh_enabled",
        False,
    ),
    ("vpn.log_cleanup", "network", "wireguard_log_cleanup_enabled", False),
    ("vpn.token_cleanup", "network", "wireguard_token_cleanup_enabled", False),
    ("crm.ticket_pull", "scheduler", "crm_ticket_pull_enabled", False),
    ("crm.work_order_pull", "scheduler", "crm_work_order_pull_enabled", False),
    (
        "crm.phase3_native_sync",
        "projects",
        "crm_phase3_native_sync_enabled",
        False,
    ),
    (
        "projects.native_read",
        "projects",
        "projects_native_read_enabled",
        False,
    ),
    ("quotes.native_read", "projects", "quotes_native_read_enabled", False),
    ("quotes.native_write", "projects", "quotes_native_write_enabled", False),
    (
        "referrals.native_read",
        "projects",
        "referrals_native_read_enabled",
        False,
    ),
    ("sales.lead_dedup", "subscriber", "lead_dedup_enabled", False),
    ("gis.sync", "gis", "sync_enabled", False),
)

# The legacy row's truthiness, as both statements below read it. One text,
# because two copies of this expression would be two answers to "was the alias
# on".
_LEGACY_TRUTH = """
        CASE
            WHEN lower(trim(both '"' from coalesce(
                legacy.value_json::text, legacy.value_text, ''
            ))) IN ('1', 'true', 'yes', 'on', 'enabled')
            THEN 'true'
            ELSE 'false'
        END
"""

# UPDATE-then-INSERT rather than `ON CONFLICT (domain, key)`, which named an
# index that no longer exists at any point in this chain: migration 507 replaced
# it with `uq_domain_settings_scope_domain_key`, and
# `001_squashed_initial_schema` builds the baseline from the CURRENT model
# metadata — so the model IS this chain's history, and dropping a constraint
# there removes it from every earlier point too. Neither statement names an
# index, so a later change to the uniqueness shape cannot break this again.
#
# `is_active IS NOT TRUE` carries over exactly: an existing canonical row that
# is already ACTIVE was set through the control plane and must not be
# overwritten by an alias.
_MATERIALIZE_UPDATE = sa.text(
    f"""
    UPDATE domain_settings AS target
       SET value_type = 'boolean',
           value_text = {_LEGACY_TRUTH},
           value_json = NULL,
           is_secret = false,
           is_active = true,
           updated_at = now()
      FROM domain_settings AS legacy
     WHERE target.domain = CAST('modules' AS settingdomain)
       AND target.key = :canonical_key
       AND target.is_active IS NOT TRUE
       AND legacy.domain = CAST(:legacy_domain AS settingdomain)
       AND legacy.key = :legacy_key
       AND legacy.is_active IS TRUE
    """
)

_MATERIALIZE_INSERT = sa.text(
    f"""
    INSERT INTO domain_settings (
        id, domain, key, value_type, value_text, value_json,
        is_secret, is_active, created_at, updated_at
    )
    SELECT
        gen_random_uuid(), CAST('modules' AS settingdomain), :canonical_key,
        'boolean',
        {_LEGACY_TRUTH},
        NULL, false, true, now(), now()
    FROM domain_settings AS legacy
    WHERE legacy.domain = CAST(:legacy_domain AS settingdomain)
      AND legacy.key = :legacy_key
      AND legacy.is_active IS TRUE
      AND NOT EXISTS (
          SELECT 1 FROM domain_settings AS existing
           WHERE existing.domain = CAST('modules' AS settingdomain)
             AND existing.key = :canonical_key
      )
    """
)

_DELETE = sa.text(
    "DELETE FROM domain_settings "
    "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
)


def upgrade() -> None:
    for canonical, legacy_domain, legacy_key, retain_legacy in _ALIASES:
        # UPDATE first, then INSERT: reversing them would have the INSERT
        # create the canonical row and the UPDATE immediately rewrite it.
        for statement in (_MATERIALIZE_UPDATE, _MATERIALIZE_INSERT):
            op.execute(
                statement.bindparams(
                    canonical_key=canonical.replace(".", "_"),
                    legacy_domain=legacy_domain,
                    legacy_key=legacy_key,
                )
            )
        if not retain_legacy:
            op.execute(_DELETE.bindparams(domain=legacy_domain, key=legacy_key))


def downgrade() -> None:
    # Intentional no-op. Recreating aliases would restore a parallel writer and
    # could overwrite decisions made through the canonical control plane.
    pass
