"""Force billing automation ON and arm the drift alarm.

Post-cutover DotMac is the biller of record, so the local billing engine must
stay on. ``ensure_by_key`` is insert-if-missing, so a seed/deploy never flips an
existing row — this migration authoritatively sets:

- ``billing.billing_enabled = true``           (master switch ON)
- ``billing.billing_enabled_expected = true``   (so check_billing_switch alarms
                                                 CRITICAL hourly if it ever drifts
                                                 OFF — i.e. it can't be silently
                                                 switched off)

value_json is cleared and value_text set to "true" to match how booleans are
stored (resolver reads value_json ?? value_text), guaranteeing TRUE regardless
of any prior value. Idempotent via ON CONFLICT.

Revision ID: 179_billing_always_on
Revises: 178_ipv6_delegated_prefixes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "179_billing_always_on"
down_revision = "178_ipv6_delegated_prefixes"
branch_labels = None
depends_on = None

_KEYS = ("billing_enabled", "billing_enabled_expected")


# UPDATE-then-INSERT rather than `ON CONFLICT (domain, key)`.
#
# A conflict TARGET must match a unique index existing when this runs, and
# `(domain, key)` no longer does: migration 507 replaced it with
# `uq_domain_settings_scope_domain_key`, which includes the scope columns. That
# reaches back to here because `001_squashed_initial_schema` builds the baseline
# from the CURRENT model metadata — the model is this chain's history, so a
# constraint dropped from the model is absent at every earlier point too.
#
# These two name no index, so no later change to the uniqueness shape can break
# this migration again.
_UPDATE = sa.text(
    """
    UPDATE domain_settings
       SET value_text = 'true',
           value_json = NULL,
           value_type = 'boolean',
           is_active = true,
           updated_at = now()
     WHERE domain = CAST(:domain AS settingdomain) AND key = :key
    """
)

_INSERT_MISSING = sa.text(
    """
    INSERT INTO domain_settings (
        id, domain, key, value_type, value_text, value_json,
        is_secret, is_active, created_at, updated_at
    )
    SELECT
        gen_random_uuid(), CAST(:domain AS settingdomain), :key, 'boolean',
        'true', NULL, false, true, now(), now()
    WHERE NOT EXISTS (
        SELECT 1 FROM domain_settings
         WHERE domain = CAST(:domain AS settingdomain) AND key = :key
    )
    """
)


def upgrade() -> None:
    # Master switch + drift-expectation (billing domain).
    for key in _KEYS:
        op.execute(_UPDATE.bindparams(domain="billing", key=key))
        op.execute(_INSERT_MISSING.bindparams(domain="billing", key=key))
    # Billing MODULE on (modules domain) — the single control plane reads this.
    op.execute(_UPDATE.bindparams(domain="modules", key="module_billing_enabled"))
    op.execute(
        _INSERT_MISSING.bindparams(domain="modules", key="module_billing_enabled")
    )


def downgrade() -> None:
    # Intentional no-op: a downgrade must never silently disable live billing.
    # Remove the pinned-expected row only, so the drift guard reverts to its
    # pre-migration (unpinned) behavior without touching the master switch.
    op.execute(
        "DELETE FROM domain_settings "
        "WHERE domain = 'billing' AND key = 'billing_enabled_expected'"
    )
